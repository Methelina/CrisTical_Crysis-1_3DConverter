#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cristical_core — Crysis 1 .chr/.cdf/.dba -> glTF 2.0 converter library
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 2.0
"""

from .crychr import read_chr, read_chr_or_cdf, read_cdf
from .crygltf import export_gltf
from .crydba import read_dba, has_tcb_controllers, read_dba_version
from .gltf_anim import GltfAnimationInjector
from .tex_convert import convert_materials
