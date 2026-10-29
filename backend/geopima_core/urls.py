from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),

    # Application APIs. config_core serves token/, token/refresh/ and me/.
    path('api/v1/tenants/', include('tenants.urls')),
    path('api/v1/properties/', include('properties.urls')),
    path('api/v1/billing/', include('billing.urls')),
    path('api/v1/mpesa/', include('mpesa.urls')),
    path('api/v1/portal/', include('portal.urls')),
    path('api/v1/', include('config_core.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)