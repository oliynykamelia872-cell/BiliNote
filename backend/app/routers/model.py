from fastapi import APIRouter
from pydantic import BaseModel

from app.exceptions.provider import ProviderError
from app.services.model import ModelService
from app.utils.response import ResponseWrapper as R
router = APIRouter()
modelService = ModelService()


class DefaultModelRequest(BaseModel):
    provider_id: str
    model_name: str


class CreateModelRequest(BaseModel):
    provider_id: str
    model_name: str

# 返回体：模型信息
class ModelItem(BaseModel):
    id: int
    model_name: str


@router.get("/default_model")
def get_default_model():
    """返回 UI 设置页写入的默认模型；未配置返回空字段。"""
    try:
        return R.success(data=ModelService.get_default_model(), msg="获取默认模型成功")
    except Exception as e:
        return R.error(str(e))


@router.post("/default_model")
def set_default_model(data: DefaultModelRequest):
    """保存默认模型：严格校验供应商启用 + 模型已登记后写入。"""
    try:
        res = ModelService.set_default_model(data.provider_id, data.model_name)
        return R.success(data=res, msg="保存默认模型成功")
    except ProviderError as e:
        return R.error(msg=e.message)
    except Exception as e:
        return R.error(str(e))


@router.get("/model_list")
def model_list():
    try:
        return R.success(modelService.get_all_models(True),msg="获取模型列表成功")
    except Exception as e:
        return R.error(e)
@router.get("/models/delete/{model_id}")
def delete_model(model_id: int):
    try:
        success = modelService.delete_model_by_id(model_id)
        if success:
            return R.success(msg="模型删除成功")
        else:
            return R.error("模型不存在或删除失败")
    except Exception as e:
        return R.error(f"删除模型失败: {e}")
@router.get("/model_list/{provider_id}")
def model_list(provider_id):

    return R.success(modelService.get_all_models_by_id(provider_id))


@router.post("/models")
def create_model(data: CreateModelRequest):
    success = ModelService.add_new_model(data.provider_id, data.model_name)
    if not success:
        return R.error("模型添加失败")
    return R.success(msg="模型添加成功")

@router.get("/model_enable/{provider_id}")
def get_enabled_models_by_provider(provider_id: str):
    try:
        models = modelService.get_enabled_models_by_provider(provider_id)
        return R.success(models, msg="获取启用模型成功")
    except Exception as e:
        return R.error(f"获取启用模型失败: {e}")
