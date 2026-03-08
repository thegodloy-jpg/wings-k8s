from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# 请求模型
class CompletionRequest(BaseModel):
    model: Optional[str] = None
    prompt: str
    max_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    max_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0


class GenerateRequest(BaseModel):
    prompt: str
    max_tokens: Optional[int] = 100
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0


# 健康检查端点
@router.get("/health")
async def health_check():
    """
    健康检查端点

    Returns:
        Dict[str, Any]: 服务状态
    """
    from app.services.engine_manager import engine_manager
    from app.services.proxy_service import proxy_service

    engine_ready = engine_manager.is_engine_ready()
    proxy_healthy = await proxy_service.health_check() if engine_ready else False

    return {
        "status": "healthy" if engine_ready and proxy_healthy else "starting",
        "engine_ready": engine_ready,
        "proxy_healthy": proxy_healthy
    }


# 补全端点
@router.post("/v1/completions")
async def create_completion(request: CompletionRequest):
    """
    创建文本补全

    Args:
        request: 补全请求

    Returns:
        Dict[str, Any]: 补全结果
    """
    from app.services.engine_manager import engine_manager
    from app.services.proxy_service import proxy_service

    if not engine_manager.is_engine_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engine is not ready yet"
        )

    try:
        request_data = {
            "prompt": request.prompt,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p
        }
        # 添加 model 参数（如果提供了）
        if request.model:
            request_data["model"] = request.model

        result = await proxy_service.forward_completion(**request_data)
        return result

    except Exception as e:
        logger.error(f"Error in completion: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# 聊天补全端点
@router.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    """
    创建聊天补全

    Args:
        request: 聊天补全请求

    Returns:
        Dict[str, Any]: 聊天补全结果
    """
    from app.services.engine_manager import engine_manager
    from app.services.proxy_service import proxy_service

    if not engine_manager.is_engine_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engine is not ready yet"
        )

    try:
        messages = [msg.dict() for msg in request.messages]
        request_data = {
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p
        }
        # 添加 model 参数（如果提供了）
        if request.model:
            request_data["model"] = request.model

        result = await proxy_service.forward_chat(**request_data)
        return result

    except Exception as e:
        logger.error(f"Error in chat completion: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# 生成端点
@router.post("/generate")
async def generate(request: GenerateRequest):
    """
    生成文本

    Args:
        request: 生成请求

    Returns:
        Dict[str, Any]: 生成结果
    """
    from app.services.engine_manager import engine_manager
    from app.services.proxy_service import proxy_service

    if not engine_manager.is_engine_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engine is not ready yet"
        )

    try:
        result = await proxy_service.forward_generate(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p
        )
        return result

    except Exception as e:
        logger.error(f"Error in generate: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


# 引擎状态端点
@router.get("/engine/status")
async def get_engine_status():
    """
    获取引擎状态

    Returns:
        Dict[str, Any]: 引擎状态信息
    """
    from app.services.engine_manager import engine_manager

    return {
        "ready": engine_manager.is_engine_ready(),
        "status": "running" if engine_manager.is_engine_ready() else "starting"
    }