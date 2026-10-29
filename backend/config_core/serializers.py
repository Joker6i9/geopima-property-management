from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import get_user_model
from .permissions import portal_role, role_names, role_permissions
from .models import AgencyProfile, UserRole, PaymentBank, SMSTemplate, AgreementTemplate

User = get_user_model()


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        
        user = self.user
        data['user'] = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'full_name': f"{user.first_name} {user.last_name}".strip() or user.username,
            'is_superuser': user.is_superuser,
            'is_staff': user.is_staff,
            'role': portal_role(user),
            'roles': sorted(role_names(user)),
            'permissions': role_permissions(user),
        }
        return data


class UserAccountSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    full_name = serializers.CharField(write_only=True, required=False)
    phone = serializers.CharField(write_only=True, required=False, allow_blank=True)
    position = serializers.CharField(write_only=True, required=False, allow_blank=True)
    role = serializers.CharField(write_only=True, required=False)
    name = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'full_name', 'phone', 'role', 'position', 'is_active', 'name', 'roles']
        extra_kwargs = {
            'email': {'required': True},
            'username': {'required': True}
        }

    def get_name(self, obj):
        return obj.get_full_name() or obj.username

    def get_roles(self, obj):
        return list(obj.roles.values_list('name', flat=True))

    def create(self, validated_data):
        role_name = validated_data.pop('role', 'Field Agent')
        full_name = validated_data.pop('full_name', '')
        phone = validated_data.pop('phone', '') or None
        validated_data.pop('position', None)
        password = validated_data.pop('password')

        name_parts = full_name.split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        # Handle user creation based on whether it's a custom user model or standard
        if hasattr(User.objects, 'create_user'):
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data['email'],
                password=password,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
            )
        else:
            user = User(
                username=validated_data['username'],
                email=validated_data['email'],
                first_name=first_name,
                last_name=last_name
            )
            user.set_password(password)
            user.save()

        if role_name:
            role, _ = UserRole.objects.get_or_create(name=role_name)
            user.roles.add(role)

        return user


class AgencyProfileSerializer(serializers.ModelSerializer):
    # Where tenants pay; set in the environment, shown here so the web app can print it on invoices.
    paybill = serializers.SerializerMethodField()

    class Meta:
        model = AgencyProfile
        fields = '__all__'

    def get_paybill(self, obj):
        from django.conf import settings
        return settings.MPESA_PAYBILL


class UserRoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserRole
        fields = ['id', 'name', 'permissions']


class PaymentBankSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentBank
        fields = '__all__'


class SMSTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSTemplate
        fields = ['id', 'category', 'template_text', 'updated_at']


class AgreementTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgreementTemplate
        fields = '__all__'