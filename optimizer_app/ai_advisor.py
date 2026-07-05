"""
optimizer_app/ai_advisor.py
Взаимодействие с DeepSeek API для получения рекомендаций по стратегии.
"""
import os
import json
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

class AIAdvisor:
    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        self.client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        self.model = model

    def get_recommendation(self, report: str, current_params: dict) -> dict:
        """
        Отправляет аналитический отчёт в DeepSeek и получает рекомендацию.
        Возвращает словарь с ключами: strategy, params, explanation, risks.
        """
        system_prompt = """
Ты — эксперт по алгоритмической торговле криптовалютами.
Твоя задача — проанализировать отчёт о работе торговой стратегии и предложить оптимальные параметры для неё.
Отвечай строго в формате JSON без лишнего текста:
{
    "strategy": "MaxTrend" или "Grid",
    "params": {
        "ema_fast": 50,
        "ema_slow": 200,
        ...
    },
    "explanation": "краткое объяснение выбора",
    "risks": "потенциальные риски"
}
Не предлагай значения, выходящие за разумные пределы.
"""

        user_prompt = f"""
Текущие параметры: {json.dumps(current_params, indent=2)}

Аналитический отчёт:
{report}
"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=1500
            )
            content = response.choices[0].message.content
            # Извлекаем JSON из ответа
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end != 0:
                json_str = content[start:end]
                recommendation = json.loads(json_str)
                return recommendation
            else:
                logger.error("Не удалось найти JSON в ответе DeepSeek")
                return {}
        except Exception as e:
            logger.error(f"Ошибка при обращении к DeepSeek: {e}")
            return {}