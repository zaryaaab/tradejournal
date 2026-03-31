from django.contrib import admin
from .models import TradingAccount, Trade, JournalEntry, Screenshot, UserProfile

admin.site.register(TradingAccount)
admin.site.register(Trade)
admin.site.register(JournalEntry)
admin.site.register(Screenshot)
admin.site.register(UserProfile)   # ← ADD THIS


from django.contrib import admin
from .models import UserProfile

