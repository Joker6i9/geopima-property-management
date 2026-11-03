from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import IntegrationStatusView, MpesaConfirmationView, MpesaTransactionViewSet, MpesaValidationView, StkPushView, StkStatusView

app_name = 'mpesa'

router = DefaultRouter()
router.register(r'transactions', MpesaTransactionViewSet, basename='transaction')

urlpatterns = [
    # Safaricom callbacks. <secret> must equal MPESA_CALLBACK_SECRET; register these full URLs with Daraja.
    path('c2b/<str:secret>/validation/', MpesaValidationView.as_view(), name='c2b_validation'),
    path('c2b/<str:secret>/confirmation/', MpesaConfirmationView.as_view(), name='c2b_confirmation'),
    path('stk/', StkPushView.as_view(), name='stk_push'),
    path('stk/<int:pk>/', StkStatusView.as_view(), name='stk_status'),
    path('integration/', IntegrationStatusView.as_view(), name='integration'),
    path('', include(router.urls)),
]
