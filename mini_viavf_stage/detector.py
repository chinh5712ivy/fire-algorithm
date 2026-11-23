import sys
from pathlib import Path
import time
import numpy as np

class Detector:
    def __init__(self, cfg):
        vendor = Path(__file__).resolve().parent/'vendor'
        sys.path.insert(0,str(vendor))
        import torch
        from yolox.exp import get_exp
        from yolox.data.data_augment import ValTransform
        from yolox.utils import postprocess
        self.torch,self.postprocess,self.cfg = torch,postprocess,cfg
        self.device = 'cuda' if cfg['device']=='auto' and torch.cuda.is_available() else cfg['device']
        if self.device=='auto': self.device='cpu'
        if self.device=='cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA requested but unavailable in current PyTorch')
        if self.device=='cpu': torch.set_num_threads(cfg['cpu_threads'])
        self.exp = get_exp(cfg['exp_file'],None)
        if (self.exp.depth,self.exp.width)!=(0.33,0.5):
            raise ValueError('Architecture must be YOLOX-S (depth .33, width .5)')
        if self.exp.num_classes!=len(cfg['class_names']):
            raise ValueError('Exp/class mapping mismatch')
        # Restrict deserialization to tensor state and the NumPy scalar metadata
        # actually present in this checkpoint. Never fall back to unsafe pickle.
        from numpy._core.multiarray import scalar
        with torch.serialization.safe_globals([scalar,np.dtype,type(np.dtype('float64')),type(np.dtype('float32'))]):
            checkpoint = torch.load(cfg['checkpoint'],map_location='cpu',weights_only=True)
        state = checkpoint.get('model',checkpoint)
        self.model = self.exp.get_model()
        self.model.load_state_dict(state,strict=True)
        self.model.to(self.device).eval()
        self.transform = ValTransform(legacy=False)
        self.last_latency_ms = 0

    def detect(self, frame, timestamp):
        start = time.perf_counter()
        image,_ = self.transform(frame,None,self.exp.test_size)
        tensor = self.torch.from_numpy(image).unsqueeze(0).float().to(self.device)
        with self.torch.inference_mode():
            output = self.postprocess(self.model(tensor),self.exp.num_classes,
                min(self.cfg['fire_confidence'],self.cfg['smoke_confidence']),self.cfg['nms_iou'],class_agnostic=False)[0]
        detections = []
        if output is not None:
            ratio = min(self.exp.test_size[0]/frame.shape[0],self.exp.test_size[1]/frame.shape[1])
            for row in output.cpu().numpy():
                cls = int(row[6]); name = self.cfg['class_names'][cls]; score = float(row[4]*row[5])
                if score<self.cfg[name+'_confidence']: continue
                b = row[:4]/ratio
                b[[0,2]] = np.clip(b[[0,2]],0,frame.shape[1]-1)
                b[[1,3]] = np.clip(b[[1,3]],0,frame.shape[0]-1)
                if b[2]<=b[0] or b[3]<=b[1]: continue
                detections.append({'class_id':cls,'class_name':name,'confidence':score,
                                   'bbox':b.tolist(),'timestamp':timestamp})
        if self.device=='cuda': self.torch.cuda.synchronize()
        self.last_latency_ms = (time.perf_counter()-start)*1000
        return detections
