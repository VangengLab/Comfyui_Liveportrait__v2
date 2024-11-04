# coding: utf-8

"""
Pipeline of LivePortrait (Animal)
"""
#from .base_config import PrintableConfig, make_abs_path
import warnings
warnings.filterwarnings("ignore", message="torch.meshgrid: in an upcoming release, it will be required to pass the indexing argument.")
warnings.filterwarnings("ignore", message="torch.utils.checkpoint: please pass in use_reentrant=True or use_reentrant=False explicitly.")
warnings.filterwarnings("ignore", message="None of the inputs have requires_grad=True. Gradients will be None")

import torch
torch.backends.cudnn.benchmark = True # disable CUDNN_BACKEND_EXECUTION_PLAN_DESCRIPTOR warning
from PIL import Image
import cv2; cv2.setNumThreads(0); cv2.ocl.setUseOpenCL(False)
import numpy as np
import os
import os.path as osp
from rich.progress import track

from .config.argument_config import ArgumentConfig
from .config.inference_config import InferenceConfig
from .config.crop_config import CropConfig
from .utils.cropper import Cropper
from .utils.camera import get_rotation_matrix
from .utils.video import images2video, concat_frames, get_fps, add_audio_to_video, has_audio_stream, video2gif
from .utils.crop import _transform_img, prepare_paste_back, paste_back
from .utils.io import load_image_rgb, load_video, resize_to_limit, dump, load
from .utils.helper import mkdir, basename, dct2device, is_video, is_template, remove_suffix, is_image, calc_motion_multiplier
from .utils.rprint import rlog as log
# from .utils.viz import viz_lmk
from .live_portrait_wrapper import LivePortraitWrapperAnimal


def make_abs_path(fn):
    return osp.join(osp.dirname(osp.realpath(__file__)), fn)

class LivePortraitPipelineAnimal(object):

    def __init__(self, inference_cfg: InferenceConfig, crop_cfg: CropConfig):
        self.live_portrait_wrapper_animal: LivePortraitWrapperAnimal = LivePortraitWrapperAnimal(inference_cfg=inference_cfg)
        self.cropper: Cropper = Cropper(crop_cfg=crop_cfg, image_type='animal_face', flag_use_half_precision=inference_cfg.flag_use_half_precision)

    def make_motion_template(self, I_lst, **kwargs):
        n_frames = I_lst.shape[0]
        template_dct = {
            'n_frames': n_frames,
            'output_fps': kwargs.get('output_fps', 25),
            'motion': [],
        }

        for i in track(range(n_frames), description='Making driving motion templates...', total=n_frames):
            # collect s, R, δ and t for inference
            I_i = I_lst[i]
            x_i_info = self.live_portrait_wrapper_animal.get_kp_info(I_i)
            R_i = get_rotation_matrix(x_i_info['pitch'], x_i_info['yaw'], x_i_info['roll'])

            item_dct = {
                'scale': x_i_info['scale'].cpu().numpy().astype(np.float32),
                'R': R_i.cpu().numpy().astype(np.float32),
                'exp': x_i_info['exp'].cpu().numpy().astype(np.float32),
                't': x_i_info['t'].cpu().numpy().astype(np.float32),
            }

            template_dct['motion'].append(item_dct)

        return template_dct

    def execute(self, args: ArgumentConfig):
        # for convenience
        inf_cfg = self.live_portrait_wrapper_animal.inference_cfg
        device = self.live_portrait_wrapper_animal.device
        crop_cfg = self.cropper.crop_cfg

        ######## load source input ########
        if is_image(args.source):
            img_rgb = load_image_rgb(args.source)
            img_rgb = resize_to_limit(img_rgb, 1280, 2)
            
        else:  # source input is an unknown format
            raise Exception(f"Unknown source format: {args.source}")

        ######## process driving info ########
        flag_load_from_template = is_template(args.driving)
        
        driving_rgb_crop_256x256_lst = None
        wfp_template = None

        if flag_load_from_template:
            # NOTE: load from template, it is fast, but the cropping video is None
            log(f"Load from template: {args.driving}, NOT the video, so the cropping video and audio are both NULL.", style='bold green')
            driving_template_dct = load(args.driving)
            #raise ValueError(driving_template_dct)
            n_frames = driving_template_dct['n_frames']

            # set output_fps
            output_fps = driving_template_dct.get('output_fps', inf_cfg.output_fps)
            log(f'The FPS of template: {output_fps}')

            if args.flag_crop_driving_video:
                log("Warning: flag_crop_driving_video is True, but the driving info is a template, so it is ignored.")

        elif osp.exists(args.driving) and is_video(args.driving):
            # load from video file, AND make motion template
            output_fps = int(get_fps(args.driving))
            log(f"Load driving video from: {args.driving}, FPS is {output_fps}")

            driving_rgb_lst = load_video(args.driving)######################################################VIDEO PROCCESS
            #raise ValueError(driving_rgb_lst)
            
            
            n_frames = len(driving_rgb_lst)

            ######## make motion template ########
            
            
            driving_rgb_crop_256x256_lst = [cv2.resize(_, (256, 256)) for _ in driving_rgb_lst]  # force to resize to 256x256
            #######################################

            # save the motion template
            I_d_lst = self.live_portrait_wrapper_animal.prepare_videos(driving_rgb_crop_256x256_lst)
            driving_template_dct = self.make_motion_template(I_d_lst, output_fps=output_fps)

            wfp_template = remove_suffix(args.driving) + '.pkl'
            dump(wfp_template, driving_template_dct)
            log(f"Dump motion template to {wfp_template}")

        else:
            raise Exception(f"{args.driving} not exists or unsupported driving info types!")

        ######## prepare for pasteback ########
        I_p_pstbk_lst = None
        
        I_p_pstbk_lst = []
        log("Prepared pasteback mask done.")

        ######## process source info ########
        
        crop_info = self.cropper.crop_source_image(img_rgb, crop_cfg)
        #print(crop_cfg)
        if crop_info is None:
            raise Exception("No animal face detected in the source image!")
        img_crop_256x256 = crop_info['img_crop_256x256']
        
        I_s = self.live_portrait_wrapper_animal.prepare_source(img_crop_256x256)
        x_s_info = self.live_portrait_wrapper_animal.get_kp_info(I_s)
        x_c_s = x_s_info['kp']
        R_s = get_rotation_matrix(x_s_info['pitch'], x_s_info['yaw'], x_s_info['roll'])
        f_s = self.live_portrait_wrapper_animal.extract_feature_3d(I_s)
        x_s = self.live_portrait_wrapper_animal.transform_keypoint(x_s_info)

        a = cv2.imread(make_abs_path("utils/resources/mask_template.png"), cv2.IMREAD_COLOR)
        a = np.array(a)
        mask_ori_float = prepare_paste_back(a, crop_info['M_c2o'], dsize=(img_rgb.shape[1], img_rgb.shape[0]))

        ######## animate ########
        
        
        
        I_p_lst = []
        for i in track(range(n_frames), description='🚀Animating...', total=n_frames):

            x_d_i_info = driving_template_dct['motion'][i]
            x_d_i_info = dct2device(x_d_i_info, device)

            R_d_i = x_d_i_info['R'] if 'R' in x_d_i_info.keys() else x_d_i_info['R_d']  # compatible with previous keys
            delta_new = x_d_i_info['exp']
            t_new = x_d_i_info['t']
            t_new[..., 2].fill_(0)  # zero tz
            scale_new = x_s_info['scale']

            x_d_i = scale_new * (x_c_s @ R_d_i + delta_new) + t_new

            if i == 0:
                x_d_0 = x_d_i
                motion_multiplier = calc_motion_multiplier(x_s, x_d_0)

            x_d_diff = (x_d_i - x_d_0) * motion_multiplier
            x_d_i = x_d_diff + x_s

            if not inf_cfg.flag_stitching:
                pass
            else:
                x_d_i = self.live_portrait_wrapper_animal.stitching(x_s, x_d_i)

            x_d_i = x_s + (x_d_i - x_s) * 1.0
            out = self.live_portrait_wrapper_animal.warp_decode(f_s, x_s, x_d_i)
            I_p_i = self.live_portrait_wrapper_animal.parse_output(out['out'])[0]
            I_p_lst.append(I_p_i)

            
            I_p_pstbk = paste_back(I_p_i, crop_info['M_c2o'], img_rgb, mask_ori_float)
            I_p_pstbk_lst.append(I_p_pstbk)

       
   

        ######### build the final concatenation result #########
        # driving frame | source image | generation
        
        # frames_concatenated = concat_frames(driving_rgb_crop_256x256_lst, [img_crop_256x256], I_p_lst)
        # frames_concatenated = np.array(frames_concatenated) 
        # # tensor = torch.from_numpy(image_np).float() / 255.0
        # frames_concatenated = torch.from_numpy(frames_concatenated).float() / 255.0
            


        
        I_p_pstbk_lst = np.array(I_p_pstbk_lst) 
        frames_concatenated = torch.from_numpy(I_p_pstbk_lst).float() / 255.0
        
        # driving_rgb_lst = np.array(driving_rgb_lst)
        # frames_concatenated = torch.from_numpy(driving_rgb_lst).float() / 255.
        
        
        

    





        #return wfp, wfp_concat, wfp_gif
        return frames_concatenated