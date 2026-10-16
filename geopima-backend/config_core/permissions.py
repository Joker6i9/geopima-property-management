from rest_framework.permissions import BasePermission

class IsSuperAdminOrAdmin(BasePermission):
    """Allows access only to Super Admins and Admins."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.user.is_superuser:
            return True
        try:
            return request.user.profile.role in ['Super Admin', 'Admin']
        except UserProfile.DoesNotExist:
            return false

class CanManageFinances(BasePermission):
    """Allows access for financial roles (Accountants, Admins, Super Admins)."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        try:
            return request.user.profile.role in ['Super Admin', 'Admin', 'Accountant']
        except UserProfile.DoesNotExist:
            return False