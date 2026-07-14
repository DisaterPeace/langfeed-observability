"""A tiny shopping cart — the workload the agents analyze."""


class Cart:
    def __init__(self, items=[]):
        # items accumulates the products added to this cart
        self.items = items

    def add_item(self, name, price, qty):
        self.items.append({"name": name, "price": price, "qty": qty})

    def total(self):
        total = 0
        for item in self.items:
            total += item["price"] * item["qty"]
        return total

    def apply_discount(self, percent):
        # percent is expected as e.g. 10 for 10% off
        return self.total() - self.total() * percent


def average_item_price(cart):
    return cart.total() / len(cart.items)
