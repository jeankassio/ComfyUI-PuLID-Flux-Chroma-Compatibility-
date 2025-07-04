import torch
from torch import nn, Tensor
from torchvision import transforms
from torchvision.transforms import functional
import os
import logging
import folder_paths
import comfy.utils
from comfy.ldm.flux.layers import timestep_embedding
from insightface.app import FaceAnalysis
from facexlib.parsing import init_parsing_model
from facexlib.utils.face_restoration_helper import FaceRestoreHelper

from .eva_clip.constants import OPENAI_DATASET_MEAN, OPENAI_DATASET_STD
from .encoders_flux import IDFormer, PerceiverAttentionCA

INSIGHTFACE_DIR = os.path.join(folder_paths.models_dir, "insightface")

MODELS_DIR = os.path.join(folder_paths.models_dir, "pulid")
if "pulid" not in folder_paths.folder_names_and_paths:
    current_paths = [MODELS_DIR]
else:
    current_paths, _ = folder_paths.folder_names_and_paths["pulid"]
folder_paths.folder_names_and_paths["pulid"] = (current_paths, folder_paths.supported_pt_extensions)

class PulidFluxModel(nn.Module):
    def __init__(self):
        super().__init__()

        self.double_interval = 2
        self.single_interval = 4

        # Init encoder
        self.pulid_encoder = IDFormer()

        # Init attention
        num_ca = 19 // self.double_interval + 38 // self.single_interval
        if 19 % self.double_interval != 0:
            num_ca += 1
        if 38 % self.single_interval != 0:
            num_ca += 1
        self.pulid_ca = nn.ModuleList([
            PerceiverAttentionCA() for _ in range(num_ca)
        ])

    def from_pretrained(self, path: str):
        state_dict = comfy.utils.load_torch_file(path, safe_load=True)
        state_dict_dict = {}
        for k, v in state_dict.items():
            module = k.split('.')[0]
            state_dict_dict.setdefault(module, {})
            new_k = k[len(module) + 1:]
            state_dict_dict[module][new_k] = v

        for module in state_dict_dict:
            getattr(self, module).load_state_dict(state_dict_dict[module], strict=True)

        del state_dict
        del state_dict_dict

    def get_embeds(self, face_embed, clip_embeds):
        return self.pulid_encoder(face_embed, clip_embeds)

def forward_orig_chroma(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    guidance: Tensor = None,
    control=None,
    transformer_options={},
    attn_mask: Tensor = None,
    **kwargs
) -> Tensor:
    """Chroma-style forward method with PuLID integration - matches original Chroma forward_orig"""
    device = comfy.model_management.get_torch_device()
    patches_replace = transformer_options.get("patches_replace", {})
    
    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")

    # running on sequences img
    img = self.img_in(img)

    # distilled vector guidance - exactly match original Chroma implementation
    mod_index_length = 344
    distill_timestep = timestep_embedding(timesteps.detach().clone(), 16).to(img.device, img.dtype)
    distil_guidance = timestep_embedding(guidance.detach().clone(), 16).to(img.device, img.dtype)

    # get all modulation index
    modulation_index = timestep_embedding(torch.arange(mod_index_length, device=img.device), 32).to(img.device, img.dtype)
    # we need to broadcast the modulation index here so each batch has all of the index
    modulation_index = modulation_index.unsqueeze(0).repeat(img.shape[0], 1, 1).to(img.device, img.dtype)
    # and we need to broadcast timestep and guidance along too
    timestep_guidance = torch.cat([distill_timestep, distil_guidance], dim=1).unsqueeze(1).repeat(1, mod_index_length, 1).to(img.dtype).to(img.device, img.dtype)
    # then and only then we could concatenate it together
    input_vec = torch.cat([timestep_guidance, modulation_index], dim=-1).to(img.device, img.dtype)

    mod_vectors = self.distilled_guidance_layer(input_vec)

    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    ca_idx = 0
    blocks_replace = patches_replace.get("dit", {})
    for i, block in enumerate(self.double_blocks):
        if i not in self.skip_mmdit:
            double_mod = (
                self.get_modulations(mod_vectors, "double_img", idx=i),
                self.get_modulations(mod_vectors, "double_txt", idx=i),
            )
            if ("double_block", i) in blocks_replace:
                def block_wrap(args):
                    out = {}
                    out["img"], out["txt"] = block(img=args["img"],
                                                   txt=args["txt"],
                                                   vec=args["vec"],
                                                   pe=args["pe"],
                                                   attn_mask=args.get("attn_mask"))
                    return out

                out = blocks_replace[("double_block", i)]({"img": img,
                                                           "txt": txt,
                                                           "vec": double_mod,
                                                           "pe": pe,
                                                           "attn_mask": attn_mask},
                                                          {"original_block": block_wrap})
                txt = out["txt"]
                img = out["img"]
            else:
                img, txt = block(img=img,
                                 txt=txt,
                                 vec=double_mod,
                                 pe=pe,
                                 attn_mask=attn_mask)

            if control is not None: # Controlnet
                control_i = control.get("input")
                if i < len(control_i):
                    add = control_i[i]
                    if add is not None:
                        img += add

        # PuLID attention for Chroma
        if self.pulid_data:
            if i % self.pulid_double_interval == 0:
                for _, node_data in self.pulid_data.items():
                    condition_start = node_data['sigma_start'] >= timesteps
                    condition_end = timesteps >= node_data['sigma_end']
                    condition = torch.logical_and(condition_start, condition_end).all()
                    
                    if condition:
                        # Ensure dtype consistency for PuLID computation
                        pulid_module = self.pulid_ca[ca_idx].to(device)
                        module_dtype = next(pulid_module.parameters()).dtype
                        embed_converted = node_data['embedding'].to(device, dtype=module_dtype)
                        img_converted = img.to(device, dtype=module_dtype)
                        pulid_out = pulid_module(embed_converted, img_converted)
                        img = img + node_data['weight'] * pulid_out.to(img.dtype)
                ca_idx += 1

    img = torch.cat((txt, img), 1)

    for i, block in enumerate(self.single_blocks):
        if i not in self.skip_dit:
            single_mod = self.get_modulations(mod_vectors, "single", idx=i)
            if ("single_block", i) in blocks_replace:
                def block_wrap(args):
                    out = {}
                    out["img"] = block(args["img"],
                                       vec=args["vec"],
                                       pe=args["pe"],
                                       attn_mask=args.get("attn_mask"))
                    return out

                out = blocks_replace[("single_block", i)]({"img": img,
                                                           "vec": single_mod,
                                                           "pe": pe,
                                                           "attn_mask": attn_mask},
                                                          {"original_block": block_wrap})
                img = out["img"]
            else:
                img = block(img, vec=single_mod, pe=pe, attn_mask=attn_mask)

            if control is not None: # Controlnet
                control_o = control.get("output")
                if i < len(control_o):
                    add = control_o[i]
                    if add is not None:
                        img[:, txt.shape[1] :, ...] += add

        # PuLID attention for single blocks in Chroma
        if self.pulid_data:
            real_img, txt = img[:, txt.shape[1]:, ...], img[:, :txt.shape[1], ...]
            if i % self.pulid_single_interval == 0:
                for _, node_data in self.pulid_data.items():
                    condition_start = node_data['sigma_start'] >= timesteps
                    condition_end = timesteps >= node_data['sigma_end']
                    condition = torch.logical_and(condition_start, condition_end).all()

                    if condition:
                        pulid_module = self.pulid_ca[ca_idx].to(device)
                        module_dtype = next(pulid_module.parameters()).dtype
                        embed_converted = node_data['embedding'].to(device, dtype=module_dtype)
                        real_img_converted = real_img.to(device, dtype=module_dtype)
                        pulid_out = pulid_module(embed_converted, real_img_converted)
                        real_img = real_img + node_data['weight'] * pulid_out.to(real_img.dtype)
                ca_idx += 1
            img = torch.cat((txt, real_img), 1)

    img = img[:, txt.shape[1] :, ...]
    final_mod = self.get_modulations(mod_vectors, "final")
    img = self.final_layer(img, vec=final_mod)  # (N, T, patch_size ** 2 * out_channels)
    return img

def forward_orig_flux(
    self,
    img: Tensor,
    img_ids: Tensor,
    txt: Tensor,
    txt_ids: Tensor,
    timesteps: Tensor,
    y: Tensor,
    guidance: Tensor = None,
    control=None,
    transformer_options={},
    attn_mask: Tensor = None,
    **kwargs  # 添加kwargs来处理任何额外的参数
) -> Tensor:
    if img.ndim != 3 or txt.ndim != 3:
        raise ValueError("Input img and txt tensors must have 3 dimensions.")
    
    device = comfy.model_management.get_torch_device()
    
    # running on sequences img
    img = self.img_in(img)
    vec = self.time_in(timestep_embedding(timesteps, 256).to(img.dtype))
    if self.params.guidance_embed:
        if guidance is None:
            raise ValueError("Didn't get guidance strength for guidance distilled model.")
        vec = vec + self.guidance_in(timestep_embedding(guidance, 256).to(img.dtype))

    vec = vec + self.vector_in(y)
    txt = self.txt_in(txt)

    ids = torch.cat((txt_ids, img_ids), dim=1)
    pe = self.pe_embedder(ids)

    ca_idx = 0
    for i, block in enumerate(self.double_blocks):
        img, txt = block(img=img, txt=txt, vec=vec, pe=pe, attn_mask=attn_mask)

        if control is not None: # Controlnet
            control_i = control.get("input")
            if i < len(control_i):
                add = control_i[i]
                if add is not None:
                    img += add

        # PuLID attention
        if self.pulid_data:
            if i % self.pulid_double_interval == 0:
                # Will calculate influence of all pulid nodes at once
                for _, node_data in self.pulid_data.items():
                    if torch.any((node_data['sigma_start'] >= timesteps) & (timesteps >= node_data['sigma_end'])):
                        pulid_module = self.pulid_ca[ca_idx].to(device)
                        # Get the dtype from the module's first parameter
                        module_dtype = next(pulid_module.parameters()).dtype
                        # Convert inputs to match module dtype
                        embed_converted = node_data['embedding'].to(device, dtype=module_dtype)
                        img_converted = img.to(device, dtype=module_dtype)
                        # Compute and convert result back to original dtype
                        pulid_out = pulid_module(embed_converted, img_converted)
                        img = img + node_data['weight'] * pulid_out.to(img.dtype)
                ca_idx += 1

    img = torch.cat((txt, img), 1)

    for i, block in enumerate(self.single_blocks):
        img = block(img, vec=vec, pe=pe, attn_mask=attn_mask)

        if control is not None: # Controlnet
            control_o = control.get("output")
            if i < len(control_o):
                add = control_o[i]
                if add is not None:
                    img[:, txt.shape[1] :, ...] += add

        # PuLID attention
        if self.pulid_data:
            real_img, txt = img[:, txt.shape[1]:, ...], img[:, :txt.shape[1], ...]
            if i % self.pulid_single_interval == 0:
                # Will calculate influence of all nodes at once
                for _, node_data in self.pulid_data.items():
                    if torch.any((node_data['sigma_start'] >= timesteps) & (timesteps >= node_data['sigma_end'])):
                        pulid_module = self.pulid_ca[ca_idx].to(device)
                        # Get the dtype from the module's first parameter
                        module_dtype = next(pulid_module.parameters()).dtype
                        # Convert inputs to match module dtype
                        embed_converted = node_data['embedding'].to(device, dtype=module_dtype)
                        real_img_converted = real_img.to(device, dtype=module_dtype)
                        # Compute and convert result back to original dtype
                        pulid_out = pulid_module(embed_converted, real_img_converted)
                        real_img = real_img + node_data['weight'] * pulid_out.to(real_img.dtype)
                ca_idx += 1
            img = torch.cat((txt, real_img), 1)

    img = img[:, txt.shape[1] :, ...]

    img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)
    return img

def tensor_to_image(tensor):
    image = tensor.mul(255).clamp(0, 255).byte().cpu()
    image = image[..., [2, 1, 0]].numpy()
    return image

def image_to_tensor(image):
    tensor = torch.clamp(torch.from_numpy(image).float() / 255., 0, 1)
    tensor = tensor[..., [2, 1, 0]]
    return tensor

def to_gray(img):
    x = 0.299 * img[:, 0:1] + 0.587 * img[:, 1:2] + 0.114 * img[:, 2:3]
    x = x.repeat(1, 3, 1, 1)
    return x

"""
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 Nodes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

class PulidFluxModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"pulid_file": (folder_paths.get_filename_list("pulid"), )}}

    RETURN_TYPES = ("PULIDFLUX",)
    FUNCTION = "load_model"
    CATEGORY = "pulid"

    def load_model(self, pulid_file):
        model_path = folder_paths.get_full_path("pulid", pulid_file)

        # Also initialize the model, takes longer to load but then it doesn't have to be done every time you change parameters in the apply node
        model = PulidFluxModel()

        logging.info("Loading PuLID-Flux model.")
        model.from_pretrained(path=model_path)

        return (model,)

class PulidFluxInsightFaceLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "provider": (["CPU", "CUDA", "ROCM"], ),
            },
        }

    RETURN_TYPES = ("FACEANALYSIS",)
    FUNCTION = "load_insightface"
    CATEGORY = "pulid"

    def load_insightface(self, provider):
        model = FaceAnalysis(name="antelopev2", root=INSIGHTFACE_DIR, providers=[provider + 'ExecutionProvider',]) # alternative to buffalo_l
        model.prepare(ctx_id=0, det_size=(640, 640))

        return (model,)

class PulidFluxEvaClipLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
        }

    RETURN_TYPES = ("EVA_CLIP",)
    FUNCTION = "load_eva_clip"
    CATEGORY = "pulid"

    def load_eva_clip(self):
        from .eva_clip.factory import create_model_and_transforms

        model, _, _ = create_model_and_transforms('EVA02-CLIP-L-14-336', 'eva_clip', force_custom_clip=True)

        model = model.visual

        eva_transform_mean = getattr(model, 'image_mean', OPENAI_DATASET_MEAN)
        eva_transform_std = getattr(model, 'image_std', OPENAI_DATASET_STD)
        if not isinstance(eva_transform_mean, (list, tuple)):
            model["image_mean"] = (eva_transform_mean,) * 3
        if not isinstance(eva_transform_std, (list, tuple)):
            model["image_std"] = (eva_transform_std,) * 3

        return (model,)

class ApplyPulidFlux:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", ),
                "pulid_flux": ("PULIDFLUX", ),
                "eva_clip": ("EVA_CLIP", ),
                "face_analysis": ("FACEANALYSIS", ),
                "image": ("IMAGE", ),
                "weight": ("FLOAT", {"default": 1.0, "min": -1.0, "max": 5.0, "step": 0.05 }),
                "start_at": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "end_at": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
            },
            "optional": {
                "attn_mask": ("MASK", ),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID"
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply_pulid_flux"
    CATEGORY = "pulid"

    def __init__(self):
        self.pulid_data_dict = None

    def apply_pulid_flux(self, model, pulid_flux, eva_clip, face_analysis, image, weight, start_at, end_at, attn_mask=None, unique_id=None):
        device = comfy.model_management.get_torch_device()
        # Why should I care what args say, when the unet model has a different dtype?!
        # Am I missing something?!
        #dtype = comfy.model_management.unet_dtype()
        dtype = model.model.diffusion_model.dtype
        # Because of 8bit models we must check what cast type does the unet uses
        # ZLUDA (Intel, AMD) & GPUs with compute capability < 8.0 don't support bfloat16 etc.
        # Issue: https://github.com/balazik/ComfyUI-PuLID-Flux/issues/6
        if model.model.manual_cast_dtype is not None:
            dtype = model.model.manual_cast_dtype

        eva_clip.to(device, dtype=dtype)
        pulid_flux.to(device, dtype=dtype)

        # TODO: Add masking support!
        if attn_mask is not None:
            if attn_mask.dim() > 3:
                attn_mask = attn_mask.squeeze(-1)
            elif attn_mask.dim() < 3:
                attn_mask = attn_mask.unsqueeze(0)
            attn_mask = attn_mask.to(device, dtype=dtype)

        image = tensor_to_image(image)

        face_helper = FaceRestoreHelper(
            upscale_factor=1,
            face_size=512,
            crop_ratio=(1, 1),
            det_model='retinaface_resnet50',
            save_ext='png',
            device=device,
        )

        face_helper.face_parse = None
        face_helper.face_parse = init_parsing_model(model_name='bisenet', device=device)

        bg_label = [0, 16, 18, 7, 8, 9, 14, 15]
        cond = []

        # Analyse multiple images at multiple sizes and combine largest area embeddings
        for i in range(image.shape[0]):
            # get insightface embeddings
            iface_embeds = None
            for size in [(size, size) for size in range(640, 256, -64)]:
                face_analysis.det_model.input_size = size
                face_info = face_analysis.get(image[i])
                if face_info:
                    # Only use the maximum face
                    # Removed the reverse=True from original code because we need the largest area not the smallest one!
                    # Sorts the list in ascending order (smallest to largest),
                    # then selects the last element, which is the largest face
                    face_info = sorted(face_info, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))[-1]
                    iface_embeds = torch.from_numpy(face_info.embedding).unsqueeze(0).to(device, dtype=dtype)
                    break
            else:
                # No face detected, skip this image
                logging.warning(f'Warning: No face detected in image {str(i)}')
                continue

            # get eva_clip embeddings
            face_helper.clean_all()
            face_helper.read_image(image[i])
            face_helper.get_face_landmarks_5(only_center_face=True)
            face_helper.align_warp_face()

            if len(face_helper.cropped_faces) == 0:
                # No face detected, skip this image
                continue

            # Get aligned face image
            align_face = face_helper.cropped_faces[0]
            # Convert bgr face image to tensor
            align_face = image_to_tensor(align_face).unsqueeze(0).permute(0, 3, 1, 2).to(device)
            parsing_out = face_helper.face_parse(functional.normalize(align_face, [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]))[0]
            parsing_out = parsing_out.argmax(dim=1, keepdim=True)
            bg = sum(parsing_out == i for i in bg_label).bool()
            white_image = torch.ones_like(align_face)
            # Only keep the face features
            face_features_image = torch.where(bg, white_image, to_gray(align_face))

            # Transform img before sending to eva_clip
            # Apparently MPS only supports NEAREST interpolation?
            face_features_image = functional.resize(face_features_image, eva_clip.image_size, transforms.InterpolationMode.BICUBIC if 'cuda' in device.type else transforms.InterpolationMode.NEAREST).to(device, dtype=dtype)
            face_features_image = functional.normalize(face_features_image, eva_clip.image_mean, eva_clip.image_std)

            # eva_clip
            id_cond_vit, id_vit_hidden = eva_clip(face_features_image, return_all_features=False, return_hidden=True, shuffle=False)
            id_cond_vit = id_cond_vit.to(device, dtype=dtype)
            for idx in range(len(id_vit_hidden)):
                id_vit_hidden[idx] = id_vit_hidden[idx].to(device, dtype=dtype)

            id_cond_vit = torch.div(id_cond_vit, torch.norm(id_cond_vit, 2, 1, True))

            # Combine embeddings
            id_cond = torch.cat([iface_embeds, id_cond_vit], dim=-1)

            # Pulid_encoder
            cond.append(pulid_flux.get_embeds(id_cond, id_vit_hidden))

        if not cond:
            # No faces detected, return the original model
            logging.warning("PuLID warning: No faces detected in any of the given images, returning unmodified model.")
            return (model,)

        # average embeddings
        cond = torch.cat(cond).to(device, dtype=dtype)
        if cond.shape[0] > 1:
            cond = torch.mean(cond, dim=0, keepdim=True)

        sigma_start = model.get_model_object("model_sampling").percent_to_sigma(start_at)
        sigma_end = model.get_model_object("model_sampling").percent_to_sigma(end_at)

        # Patch the Flux model (original diffusion_model)
        # Nah, I don't care for the official ModelPatcher because it's undocumented!
        # I want the end result now, and I don’t mind if I break other custom nodes in the process. 😄
        flux_model = model.model.diffusion_model
        # Let's see if we already patched the underlying flux model, if not apply patch
        if not hasattr(flux_model, "pulid_ca"):
            # Add perceiver attention, variables and current node data (weight, embedding, sigma_start, sigma_end)
            # The pulid_data is stored in Dict by unique node index,
            # so we can chain multiple ApplyPulidFlux nodes!
            flux_model.pulid_ca = pulid_flux.pulid_ca
            flux_model.pulid_double_interval = pulid_flux.double_interval
            flux_model.pulid_single_interval = pulid_flux.single_interval
            flux_model.pulid_data = {}
            # Replace model forward_orig with our own
            if hasattr(flux_model, 'distilled_guidance_layer'):
                # Chroma model - use Chroma-specific forward method
                new_method = forward_orig_chroma.__get__(flux_model, flux_model.__class__)
            else:
                # FLUX model - use FLUX-specific forward method
                new_method = forward_orig_flux.__get__(flux_model, flux_model.__class__)
            
            setattr(flux_model, 'forward_orig', new_method)

        # Patch is already in place, add data (weight, embedding, sigma_start, sigma_end) under unique node index
        flux_model.pulid_data[unique_id] = {
            'weight': weight,
            'embedding': cond,
            'sigma_start': sigma_start,
            'sigma_end': sigma_end,
        }

        # Keep a reference for destructor (if node is deleted the data will be deleted as well)
        self.pulid_data_dict = {'data': flux_model.pulid_data, 'unique_id': unique_id}

        return (model,)

    def __del__(self):
        # Destroy the data for this node
        if self.pulid_data_dict:
            del self.pulid_data_dict['data'][self.pulid_data_dict['unique_id']]
            del self.pulid_data_dict


NODE_CLASS_MAPPINGS = {
    "PulidFluxModelLoader": PulidFluxModelLoader,
    "PulidFluxInsightFaceLoader": PulidFluxInsightFaceLoader,
    "PulidFluxEvaClipLoader": PulidFluxEvaClipLoader,
    "ApplyPulidFlux": ApplyPulidFlux,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PulidFluxModelLoader": "Load PuLID Flux Model",
    "PulidFluxInsightFaceLoader": "Load InsightFace (PuLID Flux)",
    "PulidFluxEvaClipLoader": "Load Eva Clip (PuLID Flux)",
    "ApplyPulidFlux": "Apply PuLID Flux",
}
