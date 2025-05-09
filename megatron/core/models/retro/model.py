# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.

"""Retro Model."""
from typing import Dict, Optional

from torch import Tensor

from megatron.core.dist_checkpointing.mapping import ShardedStateDict
from megatron.core.inference.contexts import BaseInferenceContext
from megatron.core.models.gpt import GPTModel
from megatron.core.utils import deprecate_inference_params


class RetroModel(GPTModel):
    """Retro Model.

    A Retro model mostly re-uses the GPTModel interface, with the only difference
    being the embedding of the 'context' this is used by Retro for processing
    neighbor tokens. This embedded context is then forwarded to the Transformer
    Block.
    """

    def forward(
        self,
        input_ids: Tensor,
        position_ids: Tensor,
        attention_mask: Tensor,
        context_input_ids: Tensor = None,
        context_position_ids: Tensor = None,
        context_mask: Tensor = None,
        decoder_input: Tensor = None,
        labels: Tensor = None,
        inference_context: BaseInferenceContext = None,
        *,
        inference_params: Optional[BaseInferenceContext] = None,
    ) -> Tensor:
        """RetroModel forward method.

        Foward input tokens & mask, along with neighbor tokens & mask, through
        the Retro model..

        Args:
            input_ids (Tensor): Input token IDs.
            position_ids (Tensor): Input position IDs.
            attention_mask (Tensor): Input attention mask.
            context_input_ids (Tensor): Context (i.e., neighbor) token IDs.
            context_position_ids (Tensor): Context (i.e., neighbor) position IDs.
            context_mask (Tensor): Context (i.e., neighbor) attention mask.
            decoder_input (Tensor): When using pipeline parallelism, input_ids
                and position_ids will only be used on the first stage, and for
                all other stages decoder_input will be provided via communication
                from the previous stage.
            labels (Tensor): The labels of dimension [batch size, seq length].
            inference_context (BaseInferenceContext): Inference context.

        Returns:
            Output tensor of forward pass.
        """

        # Argument shapes:
        #   Notation:
        #     ns : Sequence length.
        #     bs : Batch size.
        #     d  : Hidden size.
        #     l  : Number of chunks per sample (i.e., seq_length/chunk_length).
        #     k  : Number of neighbors.
        #     r  : Number of retrieved tokens (neighbors + continuation).
        # - input_ids:   [ bs, ns ]
        # - context_ids: [ k*bs*l, r ]
        # - context:     [ r, k*bs*l, d ]
        # - output:      [ ns, bs, d ]

        inference_context = deprecate_inference_params(inference_context, inference_params)

        # Context embedding (e.g., for Retro neighbor tokens).
        if context_input_ids is not None:
            context = self.embedding(context_input_ids, context_position_ids)
        else:
            context = None

        # Call GPTModel.forward, and pass in embedded context.
        return super().forward(
            input_ids=input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            decoder_input=decoder_input,
            labels=labels,
            inference_context=inference_context,
            extra_block_kwargs={"context": context, "context_mask": context_mask},
        )

    def sharded_state_dict(
        self, prefix: str = '', sharded_offsets: tuple = (), metadata: Optional[Dict] = None
    ) -> ShardedStateDict:
        """Get sharded state dict.

        Args:
            prefix (str): Module name prefix.
            sharded_offsets (tuple): Offsets of local shard within global tensor.
            metadata (Optional[Dict]): Shard metadata.

        Returns:
            A <ShardedStateDict> ?
        """
        metadata = metadata or {}
        metadata['non_homogeneous_layers'] = True
        return super().sharded_state_dict(prefix, sharded_offsets, metadata)
