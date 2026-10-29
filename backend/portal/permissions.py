from rest_framework.permissions import BasePermission


class IsTenant(BasePermission):
    """The signed-in user is linked to a tenant record. Views then only ever read that tenant's data."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and hasattr(user, 'tenant_profile'))


class IsLandlord(BasePermission):
    """The signed-in user is linked to a landlord record."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and hasattr(user, 'landlord_profile'))
