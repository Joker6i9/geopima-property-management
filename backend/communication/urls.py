from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import SmsMessageViewSet

app_name = 'communication'

router = DefaultRouter()
router.register(r'messages', SmsMessageViewSet, basename='message')

urlpatterns = [path('', include(router.urls))]
