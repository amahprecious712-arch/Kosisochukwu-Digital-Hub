from django.contrib import admin
from .models import CourseMaterial, StudySession

@admin.register(CourseMaterial)
class CourseMaterialAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "course_title", "uploaded_at")
    search_fields = ("course_title", "student__username")

@admin.register(StudySession)
class StudySessionAdmin(admin.ModelAdmin):
    list_display = ("id", "material", "status", "created_at")
    list_filter = ("status",)