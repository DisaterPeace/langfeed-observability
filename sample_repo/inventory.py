"""A tiny inventory tracker — more code for the analyzer to summarize."""


class Inventory:
    def __init__(self):
        self.stock = {}

    def restock(self, name, amount):
        self.stock[name] = self.stock.get(name, 0) + amount

    def sell(self, name, amount):
        # reduce stock when an item is sold
        self.stock[name] = self.stock[name] - amount
        return self.stock[name]

    def is_low(self, name, threshold=5):
        # an item is "low" when its count drops below the threshold
        return self.stock.get(name, 0) < threshold

    def needs_restock(self, name):
        return self.is_low(name) == True
