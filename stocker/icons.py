"""Display-only product icons: an emoji from the product name, else from its category. Nothing is saved."""
import re

BY_NAME = [  # (word the name starts a word with, icon), checked in order
    ("ice cream", "🍨"), ("bread", "🍞"), ("loaf", "🍞"), ("bun", "🥯"), ("croissant", "🥐"), ("cake", "🍰"),
    ("coffee", "☕"), ("tea", "🍵"), ("teh", "🍵"), ("milk", "🥛"), ("yog", "🥛"), ("egg", "🥚"),
    ("cheese", "🧀"), ("butter", "🧈"), ("noodle", "🍜"), ("rice", "🍚"), ("chip", "🍟"), ("crisp", "🍟"),
    ("chocolate", "🍫"), ("biscuit", "🍪"), ("cookie", "🍪"), ("candy", "🍬"), ("sweet", "🍬"),
    ("water", "💧"), ("juice", "🧃"), ("beer", "🍺"), ("wine", "🍷"), ("drink", "🥤"), ("soda", "🥤"), ("cola", "🥤"),
    ("apple", "🍎"), ("banana", "🍌"), ("orange", "🍊"), ("grape", "🍇"), ("tomato", "🍅"), ("potato", "🥔"),
    ("onion", "🧅"), ("carrot", "🥕"), ("chicken", "🍗"), ("beef", "🥩"), ("pork", "🥓"), ("fish", "🐟"),
    ("oil", "🫒"), ("salt", "🧂"), ("sugar", "🍬"), ("flour", "🌾"), ("detergent", "🧴"), ("soap", "🧼"),
    ("shampoo", "🧴"), ("tissue", "🧻"), ("toilet", "🧻"), ("battery", "🔋"),
]
BY_CATEGORY = {"bakery": "🥐", "beverage": "🥤", "drink": "🥤", "dairy": "🥛", "frozen": "🧊", "grocery": "🛒",
               "snack": "🍿", "household": "🧽", "fruit": "🍎", "produce": "🥬", "vegetable": "🥬", "meat": "🥩",
               "seafood": "🐟", "personal": "🧴"}
DEFAULT = "📦"


def icon(name, category=None) -> str:
    text = str(name or "").lower()
    for word, emoji in BY_NAME:
        if re.search(r"\b" + word, text):
            return emoji
    cat = str(category or "").lower()
    return next((e for word, e in BY_CATEGORY.items() if word in cat), DEFAULT)
