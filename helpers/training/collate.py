import torch, logging, concurrent.futures, numpy as np
from os import environ
from helpers.training.state_tracker import StateTracker
from helpers.training.multi_process import rank_info
from helpers.image_manipulation.brightness import calculate_batch_luminance
from accelerate.logging import get_logger

logger = logging.getLogger("collate_fn")
logger.setLevel(environ.get("SIMPLETUNER_COLLATE_LOG_LEVEL", "INFO"))
rank_text = rank_info()
from torchvision.transforms import ToTensor

# Convert PIL Image to PyTorch Tensor
to_tensor = ToTensor()


def debug_log(msg: str):
    logger.debug(f"{rank_text}{msg}")


def compute_time_ids(
    original_size: tuple,
    target_size: tuple,
    weight_dtype,
    vae_downscale_factor: int = 8,
    crop_coordinates: list = None,
):
    if original_size is None or target_size is None:
        raise Exception(
            f"Cannot continue, the original_size or target_size were not provided: {original_size}, {target_size}"
        )
    logger.debug(
        f"Computing time ids for:"
        f"\n-> original_size = {original_size}"
        f"\n-> target_size = {target_size}"
    )
    # The dimensions of tensors are "transposed", as:
    # (batch_size, height, width)
    # An image would look like:
    # (width, height)
    # SDXL conditions are:
    # [h, w, h, w, h, w]
    original_width = original_size[0]
    original_height = original_size[1]
    target_width = int(target_size[2] * vae_downscale_factor)
    target_height = int(target_size[1] * vae_downscale_factor)
    final_target_size = (target_height, target_width)
    if original_width is None:
        raise ValueError("Original width must be specified.")
    if original_height is None:
        raise ValueError("Original height must be specified.")
    if crop_coordinates is None:
        raise ValueError("Crop coordinates were not collected during collate.")
    add_time_ids = list(
        (original_height, original_width) + tuple(crop_coordinates) + final_target_size
    )
    add_time_ids = torch.tensor([add_time_ids], dtype=weight_dtype)
    logger.debug(
        f"compute_time_ids returning {add_time_ids.shape} shaped time ids: {add_time_ids}"
    )
    return add_time_ids


def extract_pixel_values(examples):
    pixel_values = []
    for example in examples:
        image_data = example["image_data"]
        pixel_values.append(
            to_tensor(image_data).to(
                memory_format=torch.contiguous_format,
                dtype=StateTracker.get_vae_dtype(),
            )
        )
    return pixel_values


def extract_filepaths(examples):
    filepaths = []
    for example in examples:
        filepaths.append(example["image_path"])
    return filepaths


def fetch_latent(fp, data_backend_id: str):
    """Worker method to fetch latent for a single image."""
    debug_log(
        f" -> pull latents for fp {fp} from cache via data backend {data_backend_id}"
    )
    latent = StateTracker.get_vaecache(id=data_backend_id).retrieve_from_cache(fp)

    # Move to CPU and pin memory if it's not on the GPU
    debug_log(" -> push latents to GPU via pinned memory")
    latent = latent.to("cpu").pin_memory()
    return latent


def compute_latents(filepaths, data_backend_id: str):
    # Use a thread pool to fetch latents concurrently
    with concurrent.futures.ThreadPoolExecutor() as executor:
        latents = list(
            executor.map(fetch_latent, filepaths, [data_backend_id] * len(filepaths))
        )

    # Validate shapes
    test_shape = latents[0].shape
    for idx, latent in enumerate(latents):
        if latent.shape != test_shape:
            raise ValueError(
                f"File {filepaths[idx]} latent shape mismatch: {latent.shape} != {test_shape}"
            )

    debug_log(" -> stacking latents")
    return torch.stack(latents)


def compute_prompt_embeddings(captions, model_type: str = "sdxl"):
    debug_log(" -> get embed from cache")
    embedcache = StateTracker.get_embedcache()
    if embedcache.model_type == "sdxl":
        (
            prompt_embeds_all,
            add_text_embeds_all,
        ) = embedcache.compute_embeddings_for_sdxl_prompts(captions)
        debug_log(" -> concat embeds")
    else:
        debug_log(" -> concat embeds")
        prompt_embeds_all = embedcache.compute_embeddings_for_legacy_prompts(captions)[
            0
        ]
        print(f"Poop: {prompt_embeds_all}")
        prompt_embeds_all = torch.concat([prompt_embeds_all for _ in range(1)], dim=0)
        return prompt_embeds_all, None
    prompt_embeds_all = torch.concat([prompt_embeds_all for _ in range(1)], dim=0)
    add_text_embeds_all = torch.concat([add_text_embeds_all for _ in range(1)], dim=0)
    return prompt_embeds_all, add_text_embeds_all


def gather_conditional_size_features(examples, latents, weight_dtype):
    batch_time_ids_list = []

    for idx, example in enumerate(examples):
        # Compute time IDs for all examples
        time_ids = compute_time_ids(
            original_size=tuple(example["original_size"]),
            target_size=latents[idx].shape,
            crop_coordinates=example["crop_coordinates"],
            weight_dtype=weight_dtype,
        )

        # Overwrite with zeros if conditioning is to be dropped
        if example["drop_conditioning"]:
            time_ids = torch.zeros_like(time_ids)

        batch_time_ids_list.append(time_ids)

    return torch.stack(batch_time_ids_list, dim=0)


def check_latent_shapes(latents, filepaths):
    reference_shape = latents[0].shape
    for idx, latent in enumerate(latents):
        if latent.shape != reference_shape:
            print(f"Latent shape mismatch for file: {filepaths[idx]}")


def collate_fn(batch):
    if len(batch) != 1:
        raise ValueError(
            "This trainer is not designed to handle multiple batches in a single collate."
        )
    debug_log("Begin collate_fn on batch")

    # SDXL Dropout
    dropout_probability = StateTracker.get_args().caption_dropout_probability
    examples = batch[0]

    # Randomly drop captions/conditioning based on dropout_probability
    for example in examples:
        data_backend_id = example["data_backend_id"]
        if (
            dropout_probability > 0
            and dropout_probability is not None
            and np.random.rand() < dropout_probability
        ):
            example["instance_prompt_text"] = ""  # Drop caption
            example["drop_conditioning"] = True  # Flag to drop conditioning
        else:
            example["drop_conditioning"] = False

    debug_log("Collect luminance values")
    batch_luminance = [example["luminance"] for example in examples]
    # average it
    batch_luminance = sum(batch_luminance) / len(batch_luminance)
    debug_log("Extract filepaths")
    filepaths = extract_filepaths(examples)
    debug_log("Compute latents")
    latent_batch = compute_latents(filepaths, data_backend_id)
    debug_log("Check latents")
    check_latent_shapes(latent_batch, filepaths)

    # Compute embeddings and handle dropped conditionings
    debug_log("Extract captions")
    captions = [example["instance_prompt_text"] for example in examples]
    debug_log("Pull cached text embeds")
    prompt_embeds_all, add_text_embeds_all = compute_prompt_embeddings(captions)
    batch_time_ids = None
    if add_text_embeds_all is not None:
        debug_log("Compute and stack SDXL time ids")
        batch_time_ids = gather_conditional_size_features(
            examples, latent_batch, StateTracker.get_weight_dtype()
        )
        debug_log(f"Time ids stacked to {batch_time_ids.shape}: {batch_time_ids}")

    return {
        "latent_batch": latent_batch,
        "prompt_embeds": prompt_embeds_all,
        "add_text_embeds": add_text_embeds_all,
        "batch_time_ids": batch_time_ids,
        "batch_luminance": batch_luminance,
    }
