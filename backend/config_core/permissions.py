from rest_framework.permissions import SAFE_METHODS, BasePermission

# Staff roles, from most to least access. Names match UserRole.name.
ADMINS = ('Super Admin', 'Admin')
FINANCE = ADMINS + ('Accountant',)
PORTFOLIO = ADMINS + ('Secretary',)
STAFF = ADMINS + ('Accountant', 'Secretary', 'Field Agent')


def role_names(user) -> set[str]:
    """
    Every role a user holds. `UserRole` (User.roles) is the source of truth; Django groups are still read so accounts
    created before roles were unified keep working.
    """
    if not user or not user.is_authenticated:
        return set()
    names = set(user.roles.values_list('name', flat=True))
    names.update(user.groups.values_list('name', flat=True))
    return names


def portal_role(user) -> str | None:
    """Which portal a user belongs in: 'admin' (any staff role), 'tenant', 'landlord', or None."""
    if user.is_superuser or role_names(user) & set(STAFF):
        return 'admin'
    if hasattr(user, 'tenant_profile'):
        return 'tenant'
    if hasattr(user, 'landlord_profile'):
        return 'landlord'
    return None


def role_permissions(user) -> list[str]:
    """Permission strings granted by the user's roles."""
    granted: set[str] = set()
    for role in user.roles.all():
        granted.update(role.permissions or [])
    return sorted(granted)


class RolePermission(BasePermission):
    """
    Lets a view say who may read and who may write:

        read_roles = STAFF
        write_roles = FINANCE

    Superusers always pass. A view that sets neither is closed to everyone but superusers.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        attr = 'read_roles' if request.method in SAFE_METHODS else 'write_roles'
        allowed = getattr(view, attr, None) or ()
        return bool(role_names(user) & set(allowed))
