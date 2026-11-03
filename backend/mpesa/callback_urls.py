from django.urls import path

from .views import MpesaConfirmationView, MpesaValidationView, StkCallbackView

app_name = 'payments'

# The same Safaricom callbacks as in mpesa/urls.py under a path without the word "mpesa", which Daraja refuses in
# registered URLs. These are the addresses the system registers and sends.
urlpatterns = [
    path('c2b/<str:secret>/validation/', MpesaValidationView.as_view(), name='c2b_validation'),
    path('c2b/<str:secret>/confirmation/', MpesaConfirmationView.as_view(), name='c2b_confirmation'),
    path('c2b/<str:secret>/stk/', StkCallbackView.as_view(), name='stk_callback'),
]
