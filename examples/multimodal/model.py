# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
import warnings
import logging
from copy import deepcopy

import torch
from config import get_language_model_config, get_vision_model_config, get_vision_projection_config
from layer_specs import (get_layer_spec, get_layer_spec_te, get_mlp_module_spec, get_norm_mlp_module_spec_te,
                         get_mamba_layer_spec_te)

from megatron.core.models.multimodal.llava_model import IMAGE_TOKEN, LLaVAModel
from megatron.core.models.vision.clip_vit_model import get_num_image_embeddings
from megatron.training import get_args, get_tokenizer, print_rank_0
from megatron.training.arguments import core_transformer_config_from_args
from megatron.core.utils import log_single_rank


def model_provider(
    pre_process=True, post_process=True, add_encoder=True, add_decoder=True, parallel_output=True
) -> LLaVAModel:
    """Builds the model.

    Args:
        pre_process (bool): Include the embedding layer in the gpt decoder (used with pipeline parallelism). Defaults to True.
        post_process (bool): Include an output layer and a layernorm in the gpt decoder (used with pipeline parallelism). Defaults to True.
        add_encoder (bool): Construct the encoder module (used with pipeline parallelism). Defaults to True. When we use pipelining, the encoder
            will live on only a subset of the pipeline stages (specifically, only the first stage).
        add_decoder (bool): Construct the decoder module (used with pipeline parallelism). Defaults to True. When we use pipelining, the decoder
            will live on only a subset of the pipeline stages (specifically, every stage after the first one).
        parallel_output (bool): Enable parallel model output.

    Returns:
        model: A multimodal model.
    """
    args = get_args()
    use_te = args.use_te

    print_rank_0('building a multimodal model ...')

    num_image_embeddings = get_num_image_embeddings(
        args.img_h,
        args.img_w,
        args.patch_dim,
        args.vision_model_type,
        args.disable_vision_class_token,
        1,
        args.pixel_shuffle,
        args.use_tile_tags,
        args.max_num_tiles,
        args.tokenizer_prompt_format
    )
    old_seq_length = args.seq_length
    args.seq_length = args.encoder_seq_length = num_image_embeddings
    if old_seq_length != args.seq_length:
        log_single_rank(
            logging.getLogger(__name__),
            logging.WARNING,
            f"Changed seq_length and encoder_seq_length (vision model sequence length) from {old_seq_length} to num_image_tokens ({num_image_embeddings})"
        )

    max_num_image_embeddings = max((args.max_num_tiles + int(args.use_thumbnail)), args.num_frames) * num_image_embeddings

    assert (
        args.decoder_seq_length is not None
    ), "Please provide --decoder-seq-length to set the language model sequence length"
    assert (
        args.decoder_seq_length > max_num_image_embeddings
    ), "Language model sequence length must be greater than the maximum number of image embeddings"
    if args.decoder_seq_length > args.max_position_embeddings:
        args.max_position_embeddings = args.decoder_seq_length
        warnings.warn(
            f"Expanded max_position_embeddings to {args.max_position_embeddings} to accommodate the maximum language model sequence length"
        )

    language_model_type = args.language_model_type
    vision_model_type = args.vision_model_type

    base_config = core_transformer_config_from_args(get_args())
    base_config.language_model_type = args.language_model_type
    base_config.vision_model_type = args.vision_model_type
    base_config.calculate_per_token_loss = True

    language_config = deepcopy(base_config)
    language_config = get_language_model_config(language_config)

    if language_model_type.startswith("hf://"):
        assert args.tensor_model_parallel_size == 1, "Huggingface models do not support --tensor-model-parallel-size > 1"
        assert args.pipeline_model_parallel_size < 2, "Huggingface models do not support --pipeline-model-parallel-size > 1"
        assert not args.sequence_parallel, "Huggingface models do not support --sequence-parallel"
        assert args.context_parallel_size < 2, "Huggingface models do not support --context-parallel-size > 1"

    if language_model_type.startswith("hf://"):
        language_transformer_layer_spec = None
    elif use_te:
        # Padding mask needed for SP/CP.
        padding = args.context_parallel_size > 1 and args.sequence_parallel
        if args.language_model_type.startswith('nemotron5-hybrid'):
            language_transformer_layer_spec = get_mamba_layer_spec_te(padding=padding)
        else:
            language_transformer_layer_spec = get_layer_spec_te(
                is_vit=False, padding=padding
            )  # TENorm detects LayerNorm/RMS automatically.
    else:
        language_transformer_layer_spec = get_layer_spec(
            is_vit=False, normalization=language_config.normalization
        )

    vision_config = deepcopy(base_config)
    vision_config = get_vision_model_config(
        vision_config, apply_query_key_layer_scaling=args.apply_query_key_layer_scaling
    )
    if vision_model_type.startswith("hf://"):
        assert not args.sequence_parallel, "Huggingface models do not support --sequence-parallel"
        assert args.context_parallel_size < 2, "Huggingface models do not support --context-parallel-size > 1"

    if vision_model_type in ["clip", "siglip", "radio", "cradio-g"]:
        if use_te:
            vision_transformer_layer_spec = get_layer_spec_te(
                is_vit=True
            )  # TENorm detects LayerNorm/RMS automatically.
        else:
            vision_transformer_layer_spec = get_layer_spec(
                is_vit=True, normalization=vision_config.normalization
            )
    elif vision_model_type == "radio-g":
        if use_te:
            from radio.radio_g import get_radio_g_layer_spec_te
            vision_transformer_layer_spec = get_radio_g_layer_spec_te()  # TENorm detects LayerNorm/RMS automatically.
        else:
            from radio.radio_g import get_radio_g_layer_spec
            vision_transformer_layer_spec = get_radio_g_layer_spec(
                normalization=vision_config.normalization
            )
    elif vision_model_type == "internvit":
        from nvlm.internvit import get_internvit_layer_spec
        vision_transformer_layer_spec = get_internvit_layer_spec(use_te=use_te)
    elif vision_model_type == "internvit300M":
        from nvlm.internvit import get_internvit300M_layer_spec
        vision_transformer_layer_spec = get_internvit300M_layer_spec(use_te=use_te)
    elif vision_model_type.startswith("hf://"):
        vision_transformer_layer_spec = None
    else:
        raise RuntimeError("unsupported vision model type", vision_model_type)

    vision_projection_config = deepcopy(base_config)

    vision_projection_config = get_vision_projection_config(
        vision_projection_config, language_config.hidden_size
    )

    # Make sure vision model pipeline parallel size is not inherited from the language model pipeline parallel size.
    vision_config.pipeline_model_parallel_size = 1
    vision_projection_config.pipeline_model_parallel_size = vision_config.pipeline_model_parallel_size

    # Make sure the vision model does not inherit first and last pipeline num layers from the language model.
    vision_config.first_pipeline_num_layers = vision_config.last_pipeline_num_layers = None

    if vision_projection_config.normalization:
        vision_projection_layer_spec = get_norm_mlp_module_spec_te().submodules
    else:
        vision_projection_layer_spec = get_mlp_module_spec(use_te=use_te).submodules

    # Toggle --recompute* for the vision and language model separately.
    if args.recompute_vision:
        if vision_config.recompute_method is not None and vision_config.recompute_granularity is not None:
            vision_config.recompute_num_layers = vision_config.num_layers
    else:
        vision_config.recompute_granularity = None
        vision_config.recompute_method = None
        vision_config.recompute_num_layers = None

    vision_projection_config.recompute_granularity = None
    vision_projection_config.recompute_method = None
    vision_projection_config.recompute_num_layers = None

    # TODO: Vision model and projection do not use SP/CP yet.
    vision_config.sequence_parallel = False
    vision_config.context_parallel_size = 1
    vision_config.tp_comm_overlap = False

    vision_projection_config.sequence_parallel = False
    vision_projection_config.context_parallel_size = 1
    vision_projection_config.tp_comm_overlap = False

    tokenizer = get_tokenizer()
    image_token_index = tokenizer.convert_tokens_to_ids(IMAGE_TOKEN)
    assert image_token_index is not None, f"IMAGE_TOKEN={IMAGE_TOKEN} needs to be added using the --special-tokens arg."

    tile_tags = _get_tile_tags(args, tokenizer)

    model = LLaVAModel(
        language_transformer_config=language_config,
        language_transformer_layer_spec=language_transformer_layer_spec,
        language_vocab_size=args.padded_vocab_size,
        language_max_sequence_length=args.decoder_seq_length,
        vision_transformer_config=vision_config,
        vision_transformer_layer_spec=vision_transformer_layer_spec,
        drop_vision_class_token=args.disable_vision_class_token,
        vision_projection_config=vision_projection_config,
        vision_projection_layer_spec=vision_projection_layer_spec,
        vision_projection_type="mlp",
        allow_missing_vision_projection_checkpoint=args.allow_missing_vision_projection_checkpoint,
        parallel_output=parallel_output,
        share_embeddings_and_output_weights=not args.untie_embeddings_and_output_weights,
        language_position_embedding_type=args.position_embedding_type,
        language_rotary_percent=args.rotary_percent,
        pre_process=pre_process,
        post_process=post_process,
        add_encoder=add_encoder,
        add_decoder=add_decoder,
        img_h=args.img_h,
        img_w=args.img_w,
        patch_dim=args.patch_dim,
        language_rotary_base=args.rotary_base,
        language_rope_scaling=args.use_rope_scaling,
        hybrid_attention_ratio=args.hybrid_attention_ratio,
        hybrid_mlp_ratio=args.hybrid_mlp_ratio,
        hybrid_override_pattern=args.hybrid_override_pattern,
        fp16_lm_cross_entropy=args.fp16_lm_cross_entropy,
        image_token_index=image_token_index,
        pixel_shuffle=args.pixel_shuffle,
        tile_tags=tile_tags,
        max_num_tiles=args.max_num_tiles,
        tokenizer_type=args.tokenizer_prompt_format,
    )

    model.freeze(
        freeze_language_model=args.freeze_LM,
        freeze_vision_model=args.freeze_ViT,
        freeze_vision_projection=False,
    )

    return model


def _get_tile_tags(args, tokenizer):
    """Tile tags are used in NVLM to surround image tiles with text tags."""
    if not args.use_tile_tags:
        return None

    # We expect the tokenized length of the tags is same.
    if args.max_num_tiles < 10:
        thumbnail_tag_text = "<tile_global_thumbnail>"
        if args.tokenizer_prompt_format == "nvlm-yi-34b":
            thumbnail_tag_text = "<tile_global>"

        if args.tokenizer_prompt_format.startswith("nemotron"):
            tile_tags_text = [f"<tile_{i:02d}>" for i in range(1, args.max_num_tiles + 1)] + [thumbnail_tag_text]
        else:
            tile_tags_text = [f"<tile_{i}>" for i in range(1, args.max_num_tiles + 1)] + [thumbnail_tag_text]
    elif args.max_num_tiles <= 12:
        thumbnail_tag_text = "<tile_global_thumbnail0>"
        if args.tokenizer_prompt_format == "nvlm-yi-34b":
            thumbnail_tag_text = "<tile_global0>"
        elif args.tokenizer_prompt_format.startswith("nemotron") or args.tokenizer_prompt_format == "llama3p1":
            thumbnail_tag_text = "<tile_global_thumbnail>"
        tile_tags_text = [f"<tile_{i:02d}>" for i in range(1, args.max_num_tiles + 1)] + [thumbnail_tag_text]
    else:
        raise ValueError("We only support max_num_tiles <= 12 when using nvlm image_tag_type")

    start_idx = 0
    if tokenizer._prompt_config.has_bos:
        start_idx = 1

    # Convert to tokens [num_tiles, tile_seq_len].
    tile_tags = [tokenizer.tokenize(t)[start_idx:] for t in tile_tags_text]

    return tile_tags
