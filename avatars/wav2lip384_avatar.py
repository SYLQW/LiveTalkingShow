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
from avatars.wav2lip.models.wav2lip import Wav2Lip
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, mirror_index
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))
WAV2LIP_FACE_SIZE = 384

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

def load_avatar(avatar_id):
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    face_imgs_path = f"{avatar_path}/face_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)
    frame_list_cycle = None
    input_img_list = glob.glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    frame_list_cycle = read_imgs(input_img_list)
    input_face_list = glob.glob(os.path.join(face_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_face_list = sorted(input_face_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    face_list_cycle = read_imgs(input_face_list)

    return frame_list_cycle,face_list_cycle,coord_list_cycle

@torch.no_grad()
def warm_up(batch_size,model,modelres):
    # 预热函数
    logger.info('warmup model...')
    img_batch = torch.ones(batch_size, 6, modelres, modelres).to(device)
    mel_batch = torch.ones(batch_size, 1, 80, 16).to(device)
    model(mel_batch, img_batch)

@register("avatar", "wav2lip384")
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
        self.runtime_pads = [0, 0, 0, 0]

        self.frame_list_cycle,self.face_list_cycle,self.coord_list_cycle = avatar

        self.asr = MelASR(opt,self)
        self.asr.warm_up()
    
    def set_runtime_pads(self, pads):
        values = [int(v) for v in pads[:4]]
        while len(values) < 4:
            values.append(0)
        self.runtime_pads = [max(-300, min(300, v)) for v in values]

    def get_runtime_config(self):
        source_h, source_w = self.frame_list_cycle[0].shape[:2]
        base_bbox = self.coord_list_cycle[0]
        return {
            "pads": list(self.runtime_pads),
            "source_width": source_w,
            "source_height": source_h,
            "base_bbox": self._bbox_to_xyxy(base_bbox, [0, 0, 0, 0], source_w, source_h),
            "padded_bbox": self._bbox_to_xyxy(base_bbox, self.runtime_pads, source_w, source_h),
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
        length = len(self.face_list_cycle)
        img_batch = []
        for i in range(self.batch_size):
            idx = mirror_index(length, index + i)
            face = self.face_list_cycle[idx]
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
        bbox = self.coord_list_cycle[idx]
        combine_frame = copy.deepcopy(self.frame_list_cycle[idx])
        source_h, source_w = combine_frame.shape[:2]
        padded = self._bbox_to_xyxy(bbox, self.runtime_pads, source_w, source_h)
        x1, y1, x2, y2 = padded["x1"], padded["y1"], padded["x2"], padded["y2"]
        res_frame = cv2.resize(pred_frame.astype(np.uint8),(x2-x1,y2-y1))
        h, w = res_frame.shape[:2]
        mask = np.zeros((h, w), dtype=np.float32)
        center = (w // 2, int(h * 0.70))
        axes = (max(1, int(w * 0.12)), max(1, int(h * 0.035)))
        cv2.ellipse(mask, center, axes, 0, 0, 360, 1.0, -1)
        blur = max(3, (min(h, w) // 26) | 1)
        mask = cv2.GaussianBlur(mask, (blur, blur), 0)
        mask = np.clip(mask[..., None] * 0.75, 0.0, 1.0)
        if combine_frame.ndim == 3 and combine_frame.shape[2] == 4:
            base = combine_frame[y1:y2, x1:x2, :3].astype(np.float32)
            blended = base * (1.0 - mask) + res_frame.astype(np.float32) * mask
            combine_frame[y1:y2, x1:x2, :3] = blended.astype(np.uint8)
        else:
            base = combine_frame[y1:y2, x1:x2].astype(np.float32)
            blended = base * (1.0 - mask) + res_frame.astype(np.float32) * mask
            combine_frame[y1:y2, x1:x2] = blended.astype(np.uint8)
        return combine_frame
