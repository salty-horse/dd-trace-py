import json

import django
from django.conf.urls import include
from django.contrib.auth.models import User
from django.db import connection
from django.views.decorators.csrf import csrf_exempt
from rest_framework import routers
from rest_framework import serializers
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.decorators import authentication_classes
from rest_framework.decorators import permission_classes
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView


# django.conf.urls.url was deprecated in django 3 and removed in django 4
if django.VERSION < (4, 0, 0):
    from django.conf.urls import url as handler
else:
    from django.urls import re_path as handler


class UserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = ("url", "username", "email", "groups")


class UserViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    """

    queryset = User.objects.all().order_by("-date_joined")
    serializer_class = UserSerializer


# ASM


class CustomParser(MultiPartParser):
    """
    Custom parser that extract specific data from request
    """

    def parse(self, stream, media_type=None, parser_context=None):
        """
        Return a dataframe representing mds
        """
        parsed_files = MultiPartParser.parse(self, stream, media_type, parser_context)
        value = json.loads(parsed_files.data.get("json", "")).get("value")
        return {value: parsed_files.files.get("file1").read().decode("utf-8", errors="ignore")}


@authentication_classes([])
@permission_classes([])
class ASM_View(APIView):
    """
    ASM View with custom parser
    """

    parser_classes = (CustomParser,)

    @csrf_exempt
    def post(self, request):
        return Response({"received data form": request.data})


@authentication_classes([])
@permission_classes([])
class IASTViewSet(viewsets.ViewSet):
    """
    IAST View
    """

    @csrf_exempt
    @action(methods=["post"], detail=False, url_path="sqli", url_name="iast_sqli")
    def iast_sqli(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute(request.data["query"])
        except Exception:
            pass
        return Response({"received sqli data": request.data}, status=200)

    @csrf_exempt
    @action(methods=["post"], detail=False, url_path="sqli_complex_payload", url_name="iast_sqli_complex_payload")
    def iast_sqli_complex_payload(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute(request.data["query"]["data1"]["data2"]["data3"]["data4"])
        except Exception:
            pass
        return Response({"received sqli data": request.data}, status=200)


# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
router = routers.DefaultRouter()
router.register(r"users", UserViewSet)
router.register(r"iast", IASTViewSet, basename="iast")

urlpatterns = [
    handler(r"asm/", ASM_View.as_view()),
    handler(r"^", include(router.urls)),
    handler(r"^api-auth/", include("rest_framework.urls", namespace="rest_framework")),
]
