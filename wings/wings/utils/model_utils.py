# Copyright (c) xFusion Digital Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-

from pathlib import Path
from typing import Optional

from wings.utils.file_utils import load_json_config

# 支持的内置模型架构
_LLM_MODELS = {
    "DeepseekV3ForCausalLM": [
        "DeepSeek-R1",
        "DeepSeek-R1-0528",
        "DeepSeek-V3",
        "DeepSeek-V3-0324",
        "DeepSeek-V3.1",
        "DeepSeek-R1-w8a8",
        "DeepSeek-R1-0528-w8a8",
        "DeepSeek-V3-w8a8",
        "DeepSeek-V3-0324-w8a8",
        "DeepSeek-V3.1-w8a8"
        ],
    "Glm4ForCausalLM": [
        "GLM-4-9B-0414"
        ],
    "Qwen2ForCausalLM": [
        "DeepSeek-R1-Distill-Qwen-1.5B",
        "DeepSeek-R1-Distill-Qwen-7B",
        "DeepSeek-R1-Distill-Qwen-14B",
        "DeepSeek-R1-Distill-Qwen-32B",
        "Qwen2.5-32B-Instruct",
        "QwQ-32B"
        ],
    "Qwen3ForCausalLM": [
        "Qwen3-32B"
        ],
    "Qwen3MoeForCausalLM": [
        "Qwen3-30B-A3B",
        "Qwen3-235B-A22B"
        ],
    "LlamaForCausalLM": [
        "LLaMA3-8B",
        "DeepSeek-R1-Distill-Llama-8B",
        "DeepSeek-R1-Distill-Llama-70B"
        ]
}

_MMUM_MODELS = {
    "Qwen2_5_VLForConditionalGeneration": [
        "Qwen2.5-VL-7B-Instruct",
        "Qwen2.5-VL-72B-Instruct"
        ]
}

_EMBEDDING_MODELS = {
    "XLMRobertaModel": [
        "bge-m3"
        ],
    "BertModel": [
        "bge-large-zh-v1.5"
        ],
    "Qwen3ForCausalLM": [
        'Qwen3-Embedding-0.6B'
        ]
}

_RERANK_MODELS = {
    "XLMRobertaForSequenceClassification": [
        "bge-reranker-v2-m3", 
        "bge-reranker-large"
        ]
}


class ModelIdentifier:
    """模型识别机制"""
    def __init__(self, model_name: str, model_path: str, model_type: str):
        self.model_name = model_name
        self.model_path = Path(model_path)
        self.model_type = model_type
        self.config = load_json_config(self.model_path / "config.json")
        self.model_architecture = self.identify_model_architecture()
        self.model_quantize = self.identify_model_quantize()
        self.num_hidden_layers = self.config.get("num_hidden_layers")
        self.model_dict = {
                "llm": _LLM_MODELS,
                "mmum": _MMUM_MODELS,
                "embedding": _EMBEDDING_MODELS,
                "rerank": _RERANK_MODELS
            }

    def identify_model_architecture(self) -> Optional[str]:
        """识别模型类型"""
        # 检查 architectures 字段
        architectures = self.config.get("architectures", [])
        if architectures:
            return architectures[0] 
        else:
            return "unknown_architecture"
        
    def identify_model_type(self) -> Optional[str]:
        if self.model_type == 'auto':
            model_name = self.model_name.lower()
            for model_type, models in self.model_dict.items():
                support_model_name = []
                for lst in models.values():
                    support_model_name += [name.lower() for name in lst]
                if model_name in support_model_name:
                    return model_type
            # 如果模型名称不在支持的模型列表中，那么默认返回llm
            return "llm"
        return self.model_type
    

    def identify_model_quantize(self) -> Optional[str]:
        model_quantize = ""
        if "quantize" in self.config:
            model_quantize = self.config["quantize"]
        elif "quantization_config" in self.config:
            model_quantize = self.config["quantization_config"].get("quant_method", "")
        if model_quantize:
            return model_quantize
        else:
            return self.config.get("torch_dtype", "")

    
    def is_wings_supported(self):
        support_model_architecture = []
        for models in self.model_dict.values():
            support_model_architecture += list(models.keys())
        if self.model_architecture in support_model_architecture:
            return True
        else:
            return False
