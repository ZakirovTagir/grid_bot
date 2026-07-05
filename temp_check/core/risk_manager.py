"""
core/risk_manager.py
Управление общим риском портфеля.
"""

class RiskManager:
    def __init__(self, max_total_risk_percent: float = 20.0):
        self.max_total_risk_percent = max_total_risk_percent
        self.current_risk_percent = 0.0

    def can_open_position(self, new_risk_percent: float, equity: float) -> bool:
        """Проверяет, не превысит ли новый риск общий лимит от эквити."""
        total_after = self.current_risk_percent + new_risk_percent
        return total_after <= self.max_total_risk_percent

    def add_risk(self, risk_percent: float):
        self.current_risk_percent += risk_percent

    def remove_risk(self, risk_percent: float):
        self.current_risk_percent = max(0.0, self.current_risk_percent - risk_percent)