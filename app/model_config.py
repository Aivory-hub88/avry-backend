"""
Model Configuration - Centralized model selection and fallback logic
"""

from enum import Enum
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class ModelType(str, Enum):
    """Available model types - OpenRouter Qwen models"""
    QWEN_VL_30B_THINKING = "qwen/qwen3-vl-30b-a3b-thinking"  # Qwen 3 VL 30B Thinking (vision + reasoning)
    QWEN_VL_235B_THINKING = "qwen/qwen3-vl-235b-a22b-thinking"  # Qwen 3 VL 235B Thinking (vision + deep reasoning)
    QWEN_NEXT_80B_FREE = "qwen/qwen3-next-80b-a3b-instruct:free"  # Qwen 3 Next 80B (FREE, general purpose)
    QWEN_235B_THINKING = "qwen/qwen3-235b-a22b-thinking-2507"  # Qwen 3 235B Thinking (deep reasoning)
    QWEN_CODER_FREE = "qwen/qwen3-coder:free"  # Qwen 3 Coder (FREE, code generation)
    QWEN_4B_FREE = "qwen/qwen3-4b:free"  # Qwen 3 4B (FREE, fast, lightweight)


class ModelSelector:
    """
    Centralized model selection logic.
    
    ALLOWED MODELS (OpenRouter - Qwen 3):
    - qwen/qwen3-vl-30b-a3b-thinking: Vision + reasoning (30B)
    - qwen/qwen3-vl-235b-a22b-thinking: Vision + deep reasoning (235B)
    - qwen/qwen3-next-80b-a3b-instruct:free: General purpose (80B, FREE)
    - qwen/qwen3-235b-a22b-thinking-2507: Deep reasoning (235B)
    - qwen/qwen3-coder:free: Code generation (FREE)
    - qwen/qwen3-4b:free: Fast, lightweight (4B, FREE)
    """
    
    # Model capabilities and use cases
    MODEL_SPECS: Dict[str, Dict] = {
        ModelType.QWEN_VL_235B_THINKING: {
            "max_tokens": 8000,
            "best_for": ["vision_reasoning", "image_analysis", "deep_analysis", "blueprint_generation"],
            "timeout": 120.0,
            "temperature_default": 0.3,
            "cost": "paid"
        },
        ModelType.QWEN_VL_30B_THINKING: {
            "max_tokens": 6000,
            "best_for": ["vision_reasoning", "image_analysis", "reasoning_heavy"],
            "timeout": 90.0,
            "temperature_default": 0.3,
            "cost": "paid"
        },
        ModelType.QWEN_235B_THINKING: {
            "max_tokens": 8000,
            "best_for": ["reasoning_heavy", "deep_analysis", "complex_logic"],
            "timeout": 120.0,
            "temperature_default": 0.3,
            "cost": "paid"
        },
        ModelType.QWEN_NEXT_80B_FREE: {
            "max_tokens": 8000,
            "best_for": ["long_output", "medium_output", "console_chat", "workflow_generation", "general_purpose"],
            "timeout": 90.0,
            "temperature_default": 0.3,
            "cost": "free"
        },
        ModelType.QWEN_CODER_FREE: {
            "max_tokens": 4000,
            "best_for": ["code_generation", "code_review", "debugging"],
            "timeout": 60.0,
            "temperature_default": 0.3,
            "cost": "free"
        },
        ModelType.QWEN_4B_FREE: {
            "max_tokens": 4000,
            "best_for": ["short_output", "fast_response", "simple_tasks"],
            "timeout": 30.0,
            "temperature_default": 0.3,
            "cost": "free"
        }
    }
    
    @classmethod
    def get_model_for_task(cls, task_type: str) -> str:
        """
        Select appropriate model based on task type.
        
        Args:
            task_type: One of: 
                - "snapshot_diagnostic"
                - "deep_diagnostic"
                - "workflow_generation"
                - "console_chat"
                - "log_analysis"
                - "short_output"
                - "long_output"
                - "reasoning_heavy"
        
        Returns:
            Model identifier string
        """
        task_to_model = {
            # Diagnostic tasks
            "snapshot_diagnostic": ModelType.QWEN_NEXT_80B_FREE,
            "deep_diagnostic": ModelType.QWEN_235B_THINKING,
            "blueprint_generation": ModelType.QWEN_235B_THINKING,
            
            # Console and workflow tasks
            "workflow_generation": ModelType.QWEN_NEXT_80B_FREE,
            "console_chat": ModelType.QWEN_NEXT_80B_FREE,
            "log_analysis": ModelType.QWEN_NEXT_80B_FREE,
            
            # Code tasks
            "code_generation": ModelType.QWEN_CODER_FREE,
            "code_review": ModelType.QWEN_CODER_FREE,
            "debugging": ModelType.QWEN_CODER_FREE,
            
            # Vision tasks
            "vision_reasoning": ModelType.QWEN_VL_235B_THINKING,
            "image_analysis": ModelType.QWEN_VL_30B_THINKING,
            
            # Generic task types
            "short_output": ModelType.QWEN_4B_FREE,
            "medium_output": ModelType.QWEN_NEXT_80B_FREE,
            "long_output": ModelType.QWEN_NEXT_80B_FREE,
            "reasoning_heavy": ModelType.QWEN_235B_THINKING,
            "fast_response": ModelType.QWEN_4B_FREE,
            "general_purpose": ModelType.QWEN_NEXT_80B_FREE,
        }
        
        model = task_to_model.get(task_type, ModelType.QWEN_NEXT_80B_FREE)
        logger.info(f"Selected model {model} for task type: {task_type}")
        return model
    
    @classmethod
    def get_model_specs(cls, model: str) -> Dict:
        """Get specifications for a model."""
        return cls.MODEL_SPECS.get(model, cls.MODEL_SPECS[ModelType.QWEN_NEXT_80B_FREE])
    
    @classmethod
    def get_fallback_model(cls, primary_model: str) -> str:
        """
        Get fallback model if primary fails.
        
        Args:
            primary_model: The model that failed
        
        Returns:
            Fallback model identifier
        """
        # Vision models fallback to non-vision equivalents
        if primary_model == ModelType.QWEN_VL_235B_THINKING:
            logger.warning(f"Falling back from {primary_model} to {ModelType.QWEN_235B_THINKING}")
            return ModelType.QWEN_235B_THINKING
        
        if primary_model == ModelType.QWEN_VL_30B_THINKING:
            logger.warning(f"Falling back from {primary_model} to {ModelType.QWEN_NEXT_80B_FREE}")
            return ModelType.QWEN_NEXT_80B_FREE
        
        # Thinking models fallback to general purpose
        if primary_model == ModelType.QWEN_235B_THINKING:
            logger.warning(f"Falling back from {primary_model} to {ModelType.QWEN_NEXT_80B_FREE}")
            return ModelType.QWEN_NEXT_80B_FREE
        
        # Qwen Next 80B is the default fallback (free and capable)
        logger.warning(f"Using default fallback model: {ModelType.QWEN_NEXT_80B_FREE}")
        return ModelType.QWEN_NEXT_80B_FREE
    
    @classmethod
    def validate_model(cls, model: str) -> bool:
        """
        Validate if a model is allowed.
        
        Args:
            model: Model identifier to validate
        
        Returns:
            True if model is allowed, False otherwise
        """
        allowed_models = [
            ModelType.QWEN_VL_30B_THINKING,
            ModelType.QWEN_VL_235B_THINKING,
            ModelType.QWEN_NEXT_80B_FREE,
            ModelType.QWEN_235B_THINKING,
            ModelType.QWEN_CODER_FREE,
            ModelType.QWEN_4B_FREE
        ]
        is_valid = model in allowed_models
        
        if not is_valid:
            logger.error(f"Invalid model requested: {model}. Only {allowed_models} are allowed.")
        
        return is_valid
    
    @classmethod
    def get_all_models(cls) -> list:
        """Get list of all allowed models."""
        return [
            ModelType.QWEN_VL_30B_THINKING,
            ModelType.QWEN_VL_235B_THINKING,
            ModelType.QWEN_NEXT_80B_FREE,
            ModelType.QWEN_235B_THINKING,
            ModelType.QWEN_CODER_FREE,
            ModelType.QWEN_4B_FREE
        ]
