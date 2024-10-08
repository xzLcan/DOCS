import argparse
import glob
import logging
import math
import os
from pathlib import Path
import random
import PIL
from PIL import Image, ImageOps
import numpy
import matplotlib.pyplot as plt
from itertools import chain
from accelerate import Accelerator
from accelerate.logging import get_logger

import diffusers
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    DPMSolverMultistepScheduler,
    DiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers import StableDiffusionPipeline
from diffusers.optimization import get_scheduler
from diffusers.schedulers import LMSDiscreteScheduler

import numpy as np
from packaging import version
import PIL
from PIL import Image
from tqdm.auto import tqdm
import nltk
from nltk.corpus import wordnet

import torch
from torch import nn
import torch.nn.functional as F
import torch.utils.checkpoint
from torch.utils.data import Dataset
from torchvision import transforms
import transformers
from transformers import CLIPTextModel, CLIPTokenizer, CLIPModel, CLIPProcessor

if version.parse(version.parse(PIL.__version__).base_version) >= version.parse("9.1.0"):
    PIL_INTERPOLATION = {
        "linear": PIL.Image.Resampling.BILINEAR,
        "bilinear": PIL.Image.Resampling.BILINEAR,
        "bicubic": PIL.Image.Resampling.BICUBIC,
        "lanczos": PIL.Image.Resampling.LANCZOS,
        "nearest": PIL.Image.Resampling.NEAREST, 
    }
else:
    PIL_INTERPOLATION = {
        "linear": PIL.Image.LINEAR,
        "bilinear": PIL.Image.BILINEAR,
        "bicubic": PIL.Image.BICUBIC,
        "lanczos": PIL.Image.LANCZOS,
        "nearest": PIL.Image.NEAREST,
    }

logger = get_logger(__name__)

def parse_args():

    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument('--pretrained_model_name_or_path', type=str, default="stabilityai/stable-diffusion-2-1-base", help='The name or path of the pretrained model.')
    parser.add_argument("--attr_placeholder_token", type=str, default="<>", help="Token used as a attribute placeholder")
    parser.add_argument("--obj_placeholder_token", type=str, default="[]", help="Token used as a object placeholder")
    parser.add_argument('--train_batch_size', type=int, default=6, help='Batch size for training.')
    parser.add_argument('--train_data_dir', type=str, default='image/time/ancient_statue/0', required=False, help='Directory for training data.')
    parser.add_argument('--output_dir', type=str, default='output', required=False, help='Output directory for results.')
    parser.add_argument('--vocabulary_size', type=int, default=500, help='Size of the vocabulary.')
    parser.add_argument('--num_explanation_tokens', type=int, default=50, help='Number of explanation tokens.')
    parser.add_argument('--validation_steps', type=int, default=10, help='Number of validation steps.')
    parser.add_argument('--learning_rate_attr', type=float, default=1e-2, help='Learning rate for the attribute network.')
    parser.add_argument('--learning_rate_obj', type=float, default=1e-3, help='Learning rate for the object network.')
    parser.add_argument('--max_train_steps', type=int, default=30, help='Maximum number of training steps.')
    parser.add_argument('--seed', type=int, default=1000, help='Seed for randomness.')
    parser.add_argument('--word_size', type=int, default=22, help='Number of attribute words from LLM')
    parser.add_argument('--path_to_encoder_embeddings', type=str, default='./clip_text_encoding.pt', help='Path to the encoder embeddings.')
    parser.add_argument('--vocabulary_path', type=str, default='image/time/attr.txt', required = False, help='Path to the attribute words from LLM.')  
    parser.add_argument("--num_train_epochs", type=int, default=1000, help='How many epochs will be trained.')
    parser.add_argument("--num_attr_take", type=int, default=10, help='How many attribute words are taken into consideration in calculation.')
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        required=False,
        help=(
            "Revision of pretrained model identifier from huggingface.co/models."
        ),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=100,
        help="How many times to repeat the training data.",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images, all the images in the"
            " train/validation dataset will be resized to this resolution"
        ),
    )
    parser.add_argument(
        "--center_crop",
        action="store_true",
        help="Whether to center crop images before resizing to resolution.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help=(
            "Number of updates steps to accumulate before performing a"
            " backward/update pass."
        ),
    )
    parser.add_argument(
        "--scale_lr",
        action="store_true",
        default=False,
        help=(
            "Scale the learning rate by the number of GPUs, gradient accumulation"
            " steps, and batch size."
        ),
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="constant",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine",'
            ' "cosine_with_restarts", "polynomial", "constant",'
            ' "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps",
        type=int,
        default=500,
        help="Number of steps for the warmup in the lr scheduler.",
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the"
            " data will be loaded in the main process."
        ),
    )
    parser.add_argument(
        "--adam_beta1",
        type=float,
        default=0.9,
        help="The beta1 parameter for the Adam optimizer.",
    )
    parser.add_argument(
        "--adam_beta2",
        type=float,
        default=0.999,
        help="The beta2 parameter for the Adam optimizer.",
    )
    parser.add_argument(
        "--adam_weight_decay",
        type=float,
        default=1e-2,
        help="Weight decay to use.",
    )
    parser.add_argument(
        "--adam_epsilon",
        type=float,
        default=1e-08,
        help="Epsilon value for the Adam optimizer",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory."
            " Will default to *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default="no",
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose"
            "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
            "and an Nvidia Ampere GPU."
        ),
    )
    parser.add_argument(
        "--allow_tf32",
        action="store_true",
        help=(
            "Whether or not to allow TF32 on Ampere GPUs. Can be used to speed up"
            " training. For more information, see"
            " https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices"
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            "The integration to report the results and logs to. Supported"
            ' platforms are `"tensorboard"` (default)'
        ),
    )
    parser.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="For distributed training: local_rank",
    )

    args = parser.parse_args()

    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    if args.train_data_dir is None:
        raise ValueError("You must specify a train data directory.")

    return args

imagenet_templates_small = ["a photo of a <> []"]

def set_seed(seed=1000):

    numpy.random.seed(seed=seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

set_seed()

def decode_latents(vae, latents):
    latents = 1 / 0.18215 * latents
    image = vae.decode(latents).sample
    image = (image / 2 + 0.5).clamp(0, 1)
    image = image.permute(0, 2, 3, 1)
    return image

class CUSDataset(Dataset):

    def __init__(
        self,
        data_root,
        tokenizer,
        size=512,
        repeats=100,
        interpolation="bicubic",
        flip_p=0.5,
        split="train",
        attr_placeholder_token="*",
        obj_placeholder_token="*",
        center_crop=False,
    ):
        self.data_root = data_root
        self.tokenizer = tokenizer
        self.size = size
        self.attr_placeholder_token = attr_placeholder_token
        self.obj_placeholder_token = obj_placeholder_token
        self.center_crop = center_crop
        self.flip_p = flip_p

        self.image_paths = [
            os.path.join(self.data_root, file_path)
            for file_path in os.listdir(self.data_root)
        ]

        self.num_images = len(self.image_paths)
        self._length = self.num_images

        if split == "train":
            self._length = self.num_images * repeats

        self.interpolation = {
            "linear": PIL_INTERPOLATION["linear"],
            "bilinear": PIL_INTERPOLATION["bilinear"],
            "bicubic": PIL_INTERPOLATION["bicubic"],
            "lanczos": PIL_INTERPOLATION["lanczos"],
        }[interpolation]

        self.templates = imagenet_templates_small
        self.flip_transform = transforms.RandomHorizontalFlip(p=self.flip_p)

    def __len__(self):
        return self._length

    def __getitem__(self, i):
        example = {}
        image = Image.open(self.image_paths[i % self.num_images])

        if image.mode != "RGB":
            image = image.convert("RGB")
        if random.random() < 0.5:
            image = ImageOps.mirror(image)

        text_obj = 'a photo of a []'

        example["input_ids"] = self.tokenizer(
            self.templates,
            padding="max_length",
            truncation=True,
            max_length=self.tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids[0]
    
        example["input_ids_obj"] = self.tokenizer(
            text_obj,
            padding="max_length",
            truncation=True,
            max_length=self.tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids[0]

        img = np.array(image).astype(np.uint8)

        if self.center_crop:
            crop = min(img.shape[0], img.shape[1])
            (
                h,
                w,
            ) = (
                img.shape[0],
                img.shape[1],
            )
            img = img[
                (h - crop) // 2 : (h + crop) // 2, (w - crop) // 2 : (w + crop) // 2
            ]

        image = Image.fromarray(img)
        image = image.resize((self.size, self.size), resample=self.interpolation)

        image = self.flip_transform(image)
        image = np.array(image).astype(np.uint8)
        image = (image / 127.5 - 1.0).astype(np.float32)

        example["pixel_values"] = torch.from_numpy(image).permute(2, 0, 1)
        return example

def get_clip_encodings(data_root):
    clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to("cuda")
    clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

    image_paths = [
            f"{data_root}/{i}.jpg"
            for i in range(len(glob.glob(f"{data_root}/*.jpg")))
    ]
    images = []
    for image_p in image_paths:
        image = Image.open(image_p)

        if image.mode != "RGB":
            image = image.convert("RGB")
        images.append(image)

    images_processed = clip_processor(images=images, return_tensors="pt")["pixel_values"].cuda()
    
    target_image_encodings = clip_model.get_image_features(images_processed)
    target_image_encodings /= target_image_encodings.norm(dim=-1, keepdim=True)
    del clip_model
    torch.cuda.empty_cache()

    return target_image_encodings

def get_vocabulary_indices(
    args, target_image_encodings, tokenizer, vocabulary_size
):
    clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to("cuda")
    normalized_text_encodings = torch.load(args.path_to_encoder_embeddings)

    # Calculate cosine similarities for the average image
    mean_target_image = target_image_encodings.mean(dim=0).reshape(1, -1)
    cosine_similarities = torch.cosine_similarity(
        mean_target_image, normalized_text_encodings
    ).reshape(1, -1)
    mean_cosine = torch.mean(cosine_similarities, dim=0)
    _, sorted_indices = torch.sort(mean_cosine, descending=True)

    return sorted_indices[:vocabulary_size]

class WeightLearningNetwork(nn.Module):
    def __init__(self, embedding_dim, sequence_length, hidden_dim=512):
        super(WeightLearningNetwork, self).__init__()
        self.embedding_dim = embedding_dim
        self.sequence_length = sequence_length
        self.hidden_dim = hidden_dim
        
        self.hidden_layer = nn.Linear(embedding_dim, hidden_dim)
        self.relu = nn.ReLU()
        
        self.weight_layer = nn.Linear(hidden_dim, 1, bias=False)

    def forward(self, x):
        x_reshaped = x.view(-1, self.embedding_dim) 
        
        hidden_output = self.relu(self.hidden_layer(x_reshaped))
        
        weights = self.weight_layer(hidden_output)  
        weights = torch.sigmoid(weights) 
        weights = weights.view(-1, self.sequence_length)
        weights = weights.squeeze(0)

        return weights

nltk.download('averaged_perceptron_tagger')
nltk.download('wordnet')

def main():
    args = parse_args()
    set_seed()
    
    logging_dir = os.path.join(args.output_dir, args.logging_dir)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        logging_dir=logging_dir,
    )

    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()


    if args.output_dir is not None:
        os.makedirs(args.output_dir, exist_ok=True)

    # Load tokenizer
    tokenizer = CLIPTokenizer.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="tokenizer"
    )

    # Load scheduler and models
    noise_scheduler = DDPMScheduler.from_pretrained(
        args.pretrained_model_name_or_path, subfolder="scheduler"
    )
    text_encoder = CLIPTextModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=args.revision,
    )
    vae = AutoencoderKL.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="vae",
        revision=args.revision,
    )
    unet = UNet2DConditionModel.from_pretrained(
        args.pretrained_model_name_or_path,
        subfolder="unet",
        revision=args.revision,
    )

    # Add placeholder token in tokenizer
    num_added_tokens = tokenizer.add_tokens(args.attr_placeholder_token)
    num_added_tokens = tokenizer.add_tokens(args.obj_placeholder_token)
    placeholder_token_id = tokenizer.convert_tokens_to_ids(args.obj_placeholder_token)
    
    # Resize the token embeddings as we are adding new special tokens to the tokenizer
    text_encoder.resize_token_embeddings(len(tokenizer))

    # Freeze vae and unet
    vae.requires_grad_(False)
    unet.requires_grad_(False)
    # Freeze all parameters except for the token embeddings in text encoder
    text_encoder.text_model.encoder.requires_grad_(False)
    text_encoder.text_model.final_layer_norm.requires_grad_(False)
    text_encoder.text_model.embeddings.position_embedding.requires_grad_(False)

    if args.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True

    if args.scale_lr:
        args.learning_rate = (
            args.learning_rate
            * args.gradient_accumulation_steps
            * args.train_batch_size
            * accelerator.num_processes
        ) 

    # Initialize the MLP
    net_attr = WeightLearningNetwork(1024, args.word_size)
    net_obj = WeightLearningNetwork(1024, args.vocabulary_size)

    # Initialize the optimizer
    optimizer = torch.optim.AdamW(
        [{'params': net_attr.parameters(), 'lr': args.learning_rate_attr},
        {'params': net_obj.parameters(), 'lr': args.learning_rate_obj}], 
        betas=(args.adam_beta1, args.adam_beta2), 
        weight_decay=args.adam_weight_decay, 
        eps=args.adam_epsilon
    )

    # Create dataset and DataLoaders:
    train_dataset = CUSDataset(
        data_root=args.train_data_dir,
        tokenizer=tokenizer,
        size=args.resolution,
        attr_placeholder_token=args.attr_placeholder_token,
        obj_placeholder_token=args.obj_placeholder_token,
        repeats=args.repeats,
        center_crop=args.center_crop,
        split="train",
    )
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.train_batch_size,
        shuffle=True,
        num_workers=args.dataloader_num_workers,
    )

    # Scheduler and math around the number of training steps.
    overrode_max_train_steps = False
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps
    )
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True

    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=args.max_train_steps
        * args.gradient_accumulation_steps,
    )

    text_encoder, optimizer, train_dataloader, lr_scheduler, net_attr, net_obj = (
        accelerator.prepare(
        text_encoder, optimizer, train_dataloader, lr_scheduler, net_attr, net_obj
        )
    )

    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    unet.to(accelerator.device, dtype=weight_dtype)
    vae.to(accelerator.device, dtype=weight_dtype)

    # Recalculate the total training steps
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps
    )
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    args.num_train_epochs = math.ceil(
        args.max_train_steps / num_update_steps_per_epoch
    )

    # Initialize the trackers we use, and also store our configuration.
    if accelerator.is_main_process:
        accelerator.init_trackers("CUS", config=vars(args))

    # Train
    total_batch_size = (
        args.train_batch_size
        * accelerator.num_processes
        * args.gradient_accumulation_steps
    )

    global_step = 0
    first_epoch = 0

    progress_bar = tqdm(
        range(global_step, args.max_train_steps),
        disable=not accelerator.is_local_main_process,
    )
    progress_bar.set_description("Steps")

    # Keep original embeddings as reference
    orig_embeds_params = (
        accelerator.unwrap_model(text_encoder)
        .get_input_embeddings()
        .weight.data.clone()
    )

    norms = [i.norm().item() for i in orig_embeds_params]
    avg_norm = np.mean(norms)
    text_encoder.get_input_embeddings().weight.requires_grad_(False)

    # Get object vocabulary
    num_tokens = args.vocabulary_size
    target_image_encodings = get_clip_encodings(args.train_data_dir)
    vocabulary_indices = get_vocabulary_indices(
        args, target_image_encodings, tokenizer, num_tokens
    )
    target_image_encodings.detach_().requires_grad_(False)
    vocabulary = orig_embeds_params[vocabulary_indices]

    # Get attribute embedding
    words_attr = []
    with open(args.vocabulary_path, 'r') as file:
        for line in file:
            tokens = tokenizer.encode(line.strip(), add_special_tokens=False)
            words_attr.append(tokens)
    attr_token = torch.tensor(words_attr).squeeze(1)
    attr_embedding = orig_embeds_params[attr_token]

    # Create pipeline
    pipeline = DiffusionPipeline.from_pretrained(
        args.pretrained_model_name_or_path,
        text_encoder=accelerator.unwrap_model(text_encoder),
        tokenizer=tokenizer,
        unet=unet,
        vae=vae,
        revision=args.revision,
        torch_dtype=weight_dtype,
    )
    pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
        pipeline.scheduler.config
    )
    pipeline = pipeline.to(accelerator.device)
    pipeline.set_progress_bar_config(disable=True)

    for epoch in range(first_epoch, args.num_train_epochs):
        net_obj.train(); net_attr.train()
        for batch in train_dataloader:
            text_encoder.get_input_embeddings().weight.detach_().requires_grad_(False)
            net_attr.requires_grad_(True); net_obj.requires_grad_(True)

            # calculate current embeddings
            token_embeds = text_encoder.get_input_embeddings().weight
            alphas_obj = net_obj(vocabulary)

            mask = torch.ones(args.vocabulary_size)
            for i in range(args.vocabulary_size):
                word = tokenizer.decode(vocabulary_indices[i])
                if not nltk.pos_tag([word])[0][1].startswith('NN'):
                    mask[i] = 0
            mask = mask.to("cuda:0")
            masked_alphas_obj = alphas_obj * mask

            _, sorted_obj = torch.sort(masked_alphas_obj.abs(), descending=True)

            embedding_obj = torch.matmul(masked_alphas_obj, vocabulary)
            embedding_obj = torch.mul(embedding_obj, 1 / embedding_obj.norm())
            embedding_obj = torch.mul(embedding_obj, avg_norm)

            alphas_attr = net_attr(attr_embedding)
            _, sorted_attr = torch.sort(alphas_attr.abs(), descending =True)
            embedding_attr = torch.matmul(alphas_attr[sorted_attr[:args.num_attr_take]], attr_embedding[sorted_attr[:args.num_attr_take]])
            embedding_attr = torch.mul(embedding_attr, 1 / embedding_attr.norm())
            embedding_attr = torch.mul(embedding_attr, avg_norm)

            token_embeds[placeholder_token_id-1] = embedding_attr
            token_embeds[placeholder_token_id] = embedding_obj
            text_encoder.get_input_embeddings().weight.requires_grad_(True)

            with accelerator.accumulate([net_attr, net_obj]):
                # Convert images to latent space
                latents = (
                    vae.encode(batch["pixel_values"].to(dtype=weight_dtype))
                    .latent_dist.sample()
                    .detach()
                )
                latents = latents * 0.18215

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(
                    0,
                    noise_scheduler.config.num_train_timesteps,
                    (bsz,),
                    device=latents.device,
                )
                timesteps = timesteps.long()

                # Add noise to the latents according to the noise magnitude at each timestep
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

                # Get the text embedding for conditioning
                encoder_hidden_states = text_encoder(batch["input_ids"])[0].to(dtype=weight_dtype)
                encoder_hidden_states_obj = text_encoder(batch["input_ids_obj"])[0].to(dtype=weight_dtype)

                # Predict the noise residual
                model_pred = unet(noisy_latents, timesteps, encoder_hidden_states).sample
                model_pred_obj = unet(noisy_latents, timesteps, encoder_hidden_states_obj).sample

                # Get the target for loss depending on the prediction type
                if noise_scheduler.config.prediction_type == "epsilon":
                    target = noise
                elif noise_scheduler.config.prediction_type == "v_prediction":
                    target = noise_scheduler.get_velocity(latents, noise, timesteps)
                else:
                    raise ValueError(
                        "Unknown prediction type"
                        f" {noise_scheduler.config.prediction_type}"
                    )

                mse_loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")
                mse_loss_obj = F.mse_loss(model_pred_obj.float(), target.float(), reduction="mean")

                top_indices = [
                    sorted_obj[i].item() for i in range(args.num_explanation_tokens)
                ]

                top_embedding = torch.matmul(
                    masked_alphas_obj[top_indices], vocabulary[top_indices]
                )
                obj_loss = 1 - torch.cosine_similarity(
                    top_embedding.reshape(1, -1), embedding_obj.reshape(1, -1)
                )

                loss = mse_loss + 0.001 * obj_loss + mse_loss_obj

                accelerator.backward(loss)

                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

                # Make sure not update other embedding except for added token
                index_no_updates = torch.ones(len(tokenizer), dtype=torch.bool)
                index_no_updates[placeholder_token_id-1] = False
                index_no_updates[placeholder_token_id] = False

                with torch.no_grad():
                    accelerator.unwrap_model(text_encoder).get_input_embeddings().weight[
                        index_no_updates
                    ] = orig_embeds_params[index_no_updates]

                # Checks if the accelerator has performed an optimization step behind the scenes
                if accelerator.sync_gradients:
                    progress_bar.update(1)
                    global_step += 1

            if global_step % args.validation_steps == 0:
                token_embeds[placeholder_token_id] = top_embedding
        
                pipeline.text_encoder = accelerator.unwrap_model(text_encoder)
                text_encoder.get_input_embeddings().weight.detach_().requires_grad_(False)
                net_attr.requires_grad_(False); net_obj.requires_grad_(False)
        
            if global_step == args.max_train_steps:
                saved_data = {
                    'net_attr_state_dict': net_attr.state_dict(),
                    'net_obj_state_dict': net_obj.state_dict()
                }
                torch.save(saved_data, f"{args.output_dir}/step1_params.pt")
                break

    accelerator.end_training()

if __name__ == "__main__":
    main()
