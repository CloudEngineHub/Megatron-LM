# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.

import os
import torch
from importlib.metadata import version
from pkg_resources import packaging


def get_config_path(project_dir: str) -> str:
    '''Config copy stored within retro project dir.'''
    return os.path.join(project_dir, "config.json")


def get_gpt_data_dir(project_dir: str) -> str:
    '''Get project-relative directory of GPT bin/idx datasets.'''
    return os.path.join(project_dir, "data")


def get_dummy_mask(shape, device):
    te_version = packaging.version.Version(version("transformer-engine"))
    if te_version >= packaging.version.Version("1.3"):
        # >>>
        # raise Exception("hi.")
        # <<<
        return torch.full(
            size=shape,
            fill_value=True,
            dtype=torch.bool,
            device=device)
    else:
        raise Exception("hi.")
        return None