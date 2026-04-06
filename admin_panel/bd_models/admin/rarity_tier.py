from django.contrib import admin

from ..models import RarityTier


@admin.register(RarityTier)
class RarityTierAdmin(admin.ModelAdmin):
    list_display = ("emoji", "name", "min_percentile", "color")
    list_display_links = ("name",)
    ordering = ("-min_percentile",)
