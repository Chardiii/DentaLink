import numpy as np
import pandas as pd
from datetime import datetime, timedelta

class InventoryForecaster:
    def __init__(self, inventory_items, historical_logs):
        """
        inventory_items: list of dicts or objects with current stock, item_name, min_threshold
        historical_logs: list of past usage records (item_id, quantity_changed, timestamp)
        """
        self.inventory = inventory_items
        self.logs = pd.DataFrame(historical_logs)

    def calculate_burn_rates(self):
        """Calculates the average daily consumption rate per item."""
        if self.logs.empty:
            return {}
        
        # Ensure timestamp is datetime
        self.logs['created_at'] = pd.to_datetime(self.logs['created_at'])
        
        # Filter for usage/deductions (negative changes or specific actions)
        usage_logs = self.logs[self.logs['quantity_changed'] < 0].copy()
        usage_logs['absolute_qty'] = usage_logs['quantity_changed'].abs()
        
        burn_rates = {}
        for item_id, group in usage_logs.groupby('inventory_id'):
            # Calculate total consumed over the tracked period
            total_consumed = group['absolute_qty'].sum()
            date_range = (group['created_at'].max() - group['created_at'].min()).days
            
            # Prevent division by zero if all logs are on the same day
            days = max(date_range, 1)
            daily_rate = total_consumed / days
            burn_rates[item_id] = daily_rate
            
        return burn_rates

    def predict_stockouts(self):
        """Predicts days remaining until stock reaches zero for each item."""
        burn_rates = self.calculate_burn_rates()
        predictions = []

        for item in self.inventory:
            # Handle dictionary or object attribute access securely
            item_id = item.get('id') if isinstance(item, dict) else item.id
            item_name = item.get('item_name') if isinstance(item, dict) else item.item_name
            current_qty = item.get('quantity', 0) if isinstance(item, dict) else item.quantity
            
            rate = burn_rates.get(item_id, 0.1) # Default baseline burn rate if no history
            
            if rate > 0:
                days_left = current_qty / rate
            else:
                days_left = 999.0 # Effectively infinite if no usage
                
            predictions.append({
                'item_id': item_id,
                'item_name': item_name,
                'current_qty': current_qty,
                'daily_burn_rate': round(rate, 2),
                'estimated_days_left': round(days_left, 1),
                'projected_stockout_date': (datetime.now() + timedelta(days=days_left)).strftime('%Y-%m-%d')
            })
            
        return predictions