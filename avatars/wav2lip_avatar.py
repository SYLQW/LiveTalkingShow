###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################
#
#  Wav2Lip 数字人 — 迁移自 lipreal.py + lipasr.py
#

import math
import json
import torch
import numpy as np

import os
import time
import cv2
import glob
import pickle
import copy

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from avatars.audio_features.mel import MelASR
import asyncio
from av import AudioFrame, VideoFrame
from avatars.wav2lip.models import Wav2Lip
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, mirror_index
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))
WAV2LIP_FACE_SIZE = 256

def _parse_runtime_pads(default_pads):
    raw = os.getenv("LIVETALKING_RUNTIME_PADS", "").strip()
    if not raw:
        return list(default_pads)
    values = raw.replace("，", ",").replace(" ", ",").split(",")
    pads = []
    for value in values:
        if not value:
            continue
        pads.append(int(value))
        if len(pads) == 4:
            break
    while len(pads) < 4:
        pads.append(0)
    return [max(-300, min(300, value)) for value in pads]

def _load(checkpoint_path):
    if device == 'cuda':
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint

def load_model(path):
    model = Wav2Lip()
    logger.info("Load checkpoint from: {}".format(path))
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)

    model = model.to(device)
    return model.eval()

def _load_avatar_bundle(avatar_path):
    full_imgs_path = f"{avatar_path}/full_imgs" 
    face_imgs_path = f"{avatar_path}/face_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    metadata_path = f"{avatar_path}/metadata.json"
    
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)
    frame_list_cycle = None
    input_img_list = glob.glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    frame_list_cycle = read_imgs(input_img_list)
    input_face_list = glob.glob(os.path.join(face_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_face_list = sorted(input_face_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    face_list_cycle = read_imgs(input_face_list)
    metadata = {}
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r', encoding='utf-8-sig') as f:
            metadata = json.load(f)

    return frame_list_cycle,face_list_cycle,coord_list_cycle,metadata

def load_avatar(avatar_id):
    return _load_avatar_bundle(f"./data/avatars/{avatar_id}")

def load_motion_clips(avatar_id, root_name="speaking_actions"):
    root = f"./data/{root_name}/{avatar_id}"
    if not os.path.isdir(root):
        return {}

    clips = {}
    for action_id in sorted(os.listdir(root)):
        action_path = os.path.join(root, action_id)
        if not os.path.isdir(action_path):
            continue
        try:
            frames, faces, coords, metadata = _load_avatar_bundle(action_path)
        except Exception as exc:
            logger.warning("skip %s motion clip %s: %s", root_name, action_path, exc)
            continue
        metadata["action_id"] = action_id
        metadata.setdefault("display_name", action_id)
        clips[action_id] = {
            "frames": frames,
            "faces": faces,
            "coords": coords,
            "metadata": metadata,
        }
    return clips

def load_speaking_motion_clips(avatar_id):
    return load_motion_clips(avatar_id, "speaking_actions")

def load_idle_motion_clips(avatar_id):
    return load_motion_clips(avatar_id, "idle_actions")

@torch.no_grad()
def warm_up(batch_size,model,modelres):
    # 预热函数
    logger.info('warmup model...')
    img_batch = torch.ones(batch_size, 6, modelres, modelres).to(device)
    mel_batch = torch.ones(batch_size, 1, 80, 16).to(device)
    model(mel_batch, img_batch)

@register("avatar", "wav2lip")
class LipReal(BaseAvatar):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)

        #self.fps = opt.fps # 20 ms per frame
        
        # self.batch_size = opt.batch_size
        # self.idx = 0
        # self.res_frame_queue = Queue(self.batch_size*2)
        self.model = model
        self.face_input_size = WAV2LIP_FACE_SIZE
        self.frame_list_cycle,self.face_list_cycle,self.coord_list_cycle,self.metadata = avatar
        self.motion_clips = load_speaking_motion_clips(getattr(opt, "avatar_id", ""))
        self.idle_motion_clips = load_idle_motion_clips(getattr(opt, "avatar_id", ""))
        self.current_motion_clip_id = None
        self.current_idle_clip_id = None
        if self.motion_clips:
            logger.info("loaded speaking motion clips: %s", sorted(self.motion_clips.keys()))
        if self.idle_motion_clips:
            logger.info("loaded idle motion clips: %s", sorted(self.idle_motion_clips.keys()))
        generation_pads = self.metadata.get("generation_pads", self.metadata.get("baked_pads", [0, 0, 0, 0]))
        self.generation_pads = self._normalize_pads(generation_pads)
        self.runtime_pads = _parse_runtime_pads(self.generation_pads)
        logger.info("wav2lip generation pads: %s", self.generation_pads)
        logger.info("wav2lip current pads: %s", self.runtime_pads)

        self.asr = MelASR(opt,self)
        self.asr.warm_up()

    def _normalize_pads(self, pads):
        values = [int(v) for v in pads[:4]]
        while len(values) < 4:
            values.append(0)
        return [max(-300, min(300, v)) for v in values]

    def set_runtime_pads(self, pads):
        self.runtime_pads = self._normalize_pads(pads)

    def reload_speaking_motions(self):
        self.motion_clips = load_speaking_motion_clips(getattr(self.opt, "avatar_id", ""))
        if self.current_motion_clip_id not in self.motion_clips:
            self.current_motion_clip_id = None
        logger.info("reloaded speaking motion clips: %s", sorted(self.motion_clips.keys()))
        return self.list_speaking_motions()

    def reload_idle_motions(self):
        self.idle_motion_clips = load_idle_motion_clips(getattr(self.opt, "avatar_id", ""))
        if self.current_idle_clip_id not in self.idle_motion_clips:
            self.current_idle_clip_id = None
        logger.info("reloaded idle motion clips: %s", sorted(self.idle_motion_clips.keys()))
        return self.list_idle_motions()

    def list_speaking_motions(self):
        return [
            {
                **clip["metadata"],
                "frame_count": len(clip["frames"]),
                "current": action_id == self.current_motion_clip_id,
            }
            for action_id, clip in sorted(self.motion_clips.items())
        ]

    def list_idle_motions(self):
        return [
            {
                **clip["metadata"],
                "frame_count": len(clip["frames"]),
                "current": action_id == self.current_idle_clip_id,
            }
            for action_id, clip in sorted(self.idle_motion_clips.items())
        ]

    def set_speaking_motion(self, action_id):
        action_id = str(action_id or "").strip()
        if not action_id or action_id in {"default", "none", "null"}:
            self.current_motion_clip_id = None
            logger.info("speaking motion clip cleared")
            return {"action_id": "", "display_name": "default", "current": True}
        if action_id not in self.motion_clips:
            raise ValueError(f"speaking motion clip not found: {action_id}")
        self.current_motion_clip_id = action_id
        clip = self.motion_clips[action_id]
        logger.info("speaking motion clip selected: %s", action_id)
        return {
            **clip["metadata"],
            "frame_count": len(clip["frames"]),
            "current": True,
        }

    def set_idle_motion(self, action_id):
        action_id = str(action_id or "").strip()
        if not action_id or action_id in {"default", "none", "null"}:
            self.current_idle_clip_id = None
            logger.info("idle motion clip cleared")
            return {"action_id": "", "display_name": "固定第一帧", "current": True}
        if action_id not in self.idle_motion_clips:
            raise ValueError(f"idle motion clip not found: {action_id}")
        self.current_idle_clip_id = action_id
        clip = self.idle_motion_clips[action_id]
        logger.info("idle motion clip selected: %s", action_id)
        return {
            **clip["metadata"],
            "frame_count": len(clip["frames"]),
            "current": True,
        }

    def _active_motion_clip(self):
        if self.current_motion_clip_id:
            return self.motion_clips.get(self.current_motion_clip_id)
        return None

    def _active_idle_clip(self):
        if self.current_idle_clip_id:
            return self.idle_motion_clips.get(self.current_idle_clip_id)
        return None

    def get_avatar_length(self, speaking: bool | None = None):
        clip = self._active_motion_clip() if speaking else self._active_idle_clip()
        if clip:
            return len(clip["frames"])
        return len(self.frame_list_cycle) if speaking else 1

    def get_silent_frame(self, idx: int, audiotype: int | None = None):
        clip = self._active_idle_clip()
        if clip:
            idle_idx = mirror_index(len(clip["frames"]), idx)
            return copy.deepcopy(clip["frames"][idle_idx])
        return copy.deepcopy(self.frame_list_cycle[0])

    def _paste_delta_pads(self):
        return [current - base for current, base in zip(self.runtime_pads, self.generation_pads)]

    def get_runtime_config(self):
        source_h, source_w = self.frame_list_cycle[0].shape[:2]
        base_bbox = self.coord_list_cycle[0]
        paste_delta_pads = self._paste_delta_pads()
        return {
            "pads": list(self.runtime_pads),
            "generation_pads": list(self.generation_pads),
            "baked_pads": list(self.generation_pads),
            "paste_delta_pads": paste_delta_pads,
            "effective_pads": list(self.runtime_pads),
            "source_width": source_w,
            "source_height": source_h,
            "base_bbox": self._bbox_to_xyxy(base_bbox, [0, 0, 0, 0], source_w, source_h),
            "padded_bbox": self._bbox_to_xyxy(base_bbox, paste_delta_pads, source_w, source_h),
        }

    def _bbox_to_xyxy(self, bbox, pads, source_w, source_h):
        y1, y2, x1, x2 = bbox
        top, bottom, left, right = pads
        y1 = max(0, min(source_h - 1, int(y1) - top))
        y2 = max(y1 + 1, min(source_h, int(y2) + bottom))
        x1 = max(0, min(source_w - 1, int(x1) - left))
        x2 = max(x1 + 1, min(source_w, int(x2) + right))
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    
    def inference_batch(self, index, audiofeat_batch):
        # 这里的 index 是针对当前 avatar 的索引
        # 返回一个 batch 的推理结果，batch 大小由 self.batch_size 决定
        clip = self._active_motion_clip()
        face_list_cycle = clip["faces"] if clip else self.face_list_cycle
        length = len(face_list_cycle)
        img_batch = []
        for i in range(self.batch_size):
            idx = mirror_index(length, index + i)
            face = face_list_cycle[idx]
            if face.ndim == 3 and face.shape[2] == 4:
                face = cv2.cvtColor(face, cv2.COLOR_BGRA2BGR)
            if face.shape[:2] != (self.face_input_size, self.face_input_size):
                face = cv2.resize(face, (self.face_input_size, self.face_input_size))
            img_batch.append(face)
        img_batch, audiofeat_batch = np.asarray(img_batch), np.asarray(audiofeat_batch)

        img_masked = img_batch.copy()
        img_masked[:, self.face_input_size//2:] = 0

        img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.
        audiofeat_batch = np.reshape(audiofeat_batch, [len(audiofeat_batch), audiofeat_batch.shape[1], audiofeat_batch.shape[2], 1])
        
        img_batch = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
        audiofeat_batch = torch.FloatTensor(np.transpose(audiofeat_batch, (0, 3, 1, 2))).to(device)

        with torch.no_grad():
            pred = self.model(audiofeat_batch, img_batch)
        pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
        return pred

    def paste_back_frame(self,pred_frame,idx:int):
        clip = self._active_motion_clip()
        if clip:
            bbox = clip["coords"][idx]
            combine_frame = copy.deepcopy(clip["frames"][idx])
            paste_pads = [0, 0, 0, 0]
        else:
            bbox = self.coord_list_cycle[idx]
            combine_frame = copy.deepcopy(self.frame_list_cycle[idx])
            paste_pads = self._paste_delta_pads()
        source_h, source_w = combine_frame.shape[:2]
        padded = self._bbox_to_xyxy(bbox, paste_pads, source_w, source_h)
        x1, y1, x2, y2 = padded["x1"], padded["y1"], padded["x2"], padded["y2"]
        res_frame = cv2.resize(pred_frame.astype(np.uint8),(x2-x1,y2-y1))
        if combine_frame.ndim == 3 and combine_frame.shape[2] == 4:
            combine_frame[y1:y2, x1:x2, :3] = res_frame
        else:
            combine_frame[y1:y2, x1:x2] = res_frame
        return combine_frame
