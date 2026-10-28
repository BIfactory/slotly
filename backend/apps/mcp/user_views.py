from __future__ import annotations

from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers, status

from .models import APIToken, MCPCall
from .tools import TOOL_SCOPES


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

class TokenSerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = APIToken
        fields = (
            "id",
            "name",
            "prefix",
            "scopes",
            "created_at",
            "last_used_at",
            "revoked_at",
            "is_active",
        )
        read_only_fields = fields


class TokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=False,
    )

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Name may not be blank.")
        return value

    def validate_scopes(self, value):
        invalid = [s for s in value if s not in TOOL_SCOPES]
        if invalid:
            raise serializers.ValidationError(
                f"Unknown scope(s): {invalid}. Valid scopes: {list(TOOL_SCOPES)}"
            )
        return value


class MCPCallSerializer(serializers.ModelSerializer):
    class Meta:
        model = MCPCall
        fields = (
            "id",
            "capability",
            "status",
            "duration_ms",
            "error_code",
            "created_at",
        )
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class TokenListCreateView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tokens = APIToken.objects.filter(user=request.user).order_by("-created_at")
        data = TokenSerializer(tokens, many=True).data
        return Response(data)

    def post(self, request):
        ser = TokenCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token, raw = APIToken.create_for_user(
            user=request.user,
            name=ser.validated_data["name"],
            scopes=ser.validated_data["scopes"],
        )
        data = TokenSerializer(token).data
        data["token"] = raw
        return Response(data, status=status.HTTP_201_CREATED)


class TokenRevokeView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk: int):
        try:
            token = APIToken.objects.get(pk=pk, user=request.user)
        except APIToken.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TokenCallsView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        try:
            token = APIToken.objects.get(pk=pk, user=request.user)
        except APIToken.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        calls = MCPCall.objects.filter(token=token).order_by("-created_at")[:50]
        data = MCPCallSerializer(calls, many=True).data
        return Response(data)
