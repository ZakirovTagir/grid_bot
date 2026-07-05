# Минимальные размеры ордеров и шаг цены (для Bybit USDT фьючерсов)
# Данные можно обновлять через API, здесь статический пример
PAIRS_CONFIG = {
    "BTCUSDT": {"min_qty": 0.001, "price_step": 0.5, "min_order_usdt": 5.0},
    "ETHUSDT": {"min_qty": 0.01, "price_step": 0.05, "min_order_usdt": 5.0},
    "SOLUSDT": {"min_qty": 0.1, "price_step": 0.01, "min_order_usdt": 5.0},
    "ARBUSDT": {"min_qty": 0.1, "price_step": 0.001, "min_order_usdt": 5.0},
    "DOGEUSDT": {"min_qty": 1.0, "price_step": 0.00001, "min_order_usdt": 5.0},
    "LINKUSDT": {"min_qty": 0.1, "price_step": 0.001, "min_order_usdt": 5.0},
   # "MATICUSDT": {"min_qty": 0.1, "price_step": 0.0001, "min_order_usdt": 5.0},
    "AVAXUSDT": {"min_qty": 0.1, "price_step": 0.01, "min_order_usdt": 5.0},
    "APTUSDT": {"min_qty": 0.1, "price_step": 0.001, "min_order_usdt": 5.0},
    "FILUSDT": {"min_qty": 0.1, "price_step": 0.001, "min_order_usdt": 5.0},
}