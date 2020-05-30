from kdkit.models.adaptation import ADAPTATION_CLASS_DICT
from kdkit.models.custom import CUSTOM_MODEL_CLASS_DICT, CUSTOM_MODEL_FUNC_DICT
from kdkit.models.special import SPECIAL_CLASS_DICT

MODEL_DICT = dict()

MODEL_DICT.update(ADAPTATION_CLASS_DICT)
MODEL_DICT.update(SPECIAL_CLASS_DICT)
MODEL_DICT.update(CUSTOM_MODEL_CLASS_DICT)
MODEL_DICT.update(CUSTOM_MODEL_FUNC_DICT)