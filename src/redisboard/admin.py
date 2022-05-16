from functools import partial
from functools import wraps
from logging import getLogger
from typing import Union
from urllib.parse import quote
from urllib.parse import unquote_to_bytes

from django.contrib import admin
from django.http import HttpResponseForbidden
from django.http import HttpResponseNotFound
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.shortcuts import resolve_url
from django.template.response import TemplateResponse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .data import REDISBOARD_SCAN_COUNT
from .models import RedisServer
from .structs import DBInfo

logger = getLogger(__name__)


def cleanup_changelist_response(response: TemplateResponse):
    obj: RedisServer
    for obj in response.context_data['cl'].queryset:
        connection = obj.__dict__.get('connection')
        if connection:
            connection.close()


def cleanup_connection(response: TemplateResponse, connection):
    connection.close()


class RedisServerAdmin(admin.ModelAdmin):
    class Media:
        css = {'all': ('redisboard/admin.css',)}

    list_display = (
        'display_name',
        'status',
        'memory',
        'clients',
        'details',
        'cpu_utilization',
        'slowlog',
        'tools',
    )

    list_filter = ('label',)

    def display_name(self, obj: RedisServer):
        return str(obj)

    display_name.ordering = 'label', 'url'
    display_name.short_description = _('Name')

    def details(self, obj: RedisServer):
        return obj.display.details()

    details.short_description = _('Details')

    def cpu_utilization(self, obj: RedisServer):
        return obj.display.cpu()

    cpu_utilization.short_description = _('CPU Utilization')

    def slowlog(self, obj: RedisServer):
        return obj.display.slowlog()

    slowlog.short_description = _('Slowlog')

    def status(self, obj: RedisServer):
        return obj.stats.status

    status.short_description = _('Status')

    def memory(self, obj: RedisServer):
        return obj.stats.memory

    memory.short_description = _('Memory')

    def clients(self, obj: RedisServer):
        return obj.stats.clients

    clients.short_description = _('Clients')

    def tools(self, obj: RedisServer):
        return format_html(
            '<a href="{}">{}</a>',
            resolve_url('admin:redisboard_redisserver_inspect', server_id=obj.id),
            _('Inspect'),
        )

    tools.short_description = _('Tools')

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        if isinstance(response, TemplateResponse):
            response.add_post_render_callback(cleanup_changelist_response)
        return response

    def get_urls(self):
        urlpatterns = super(RedisServerAdmin, self).get_urls()
        from django.urls import path

        def wrap(view):
            @wraps(view)
            @self.admin_site.admin_view
            def wrapper(request, server_id, **kwargs):
                server = get_object_or_404(RedisServer, id=server_id)
                if self.has_view_permission(request, server) and request.user.has_perm('redisboard.can_inspect'):
                    connection = server.connection
                    try:
                        response = view(request, server, **kwargs)
                        if isinstance(response, TemplateResponse):
                            response.add_post_render_callback(partial(cleanup_connection, connection=connection.close))
                        return response
                    finally:
                        connection.close()
                else:
                    return HttpResponseForbidden("You can't inspect this server.")

            return wrapper

        return [
            path(
                '<int:server_id>/inspect/',
                wrap(self.inspect),
                name='redisboard_redisserver_inspect',
            ),
            path(
                '<int:server_id>/inspect/<int:db>/',
                wrap(self.inspect),
                name='redisboard_redisserver_inspect',
            ),
            path(
                '<int:server_id>/inspect/<int:db>/key/<path:key>/',
                wrap(self.inspect_key),
                name='redisboard_redisserver_inspect',
            ),
            path(
                '<int:server_id>/inspect/<int:db>/<int:cursor>/',
                wrap(self.inspect),
                name='redisboard_redisserver_inspect',
            ),
            path(
                '<int:server_id>/inspect/<int:db>/<int:cursor>/key/<path:key>/',
                wrap(self.inspect_key),
                name='redisboard_redisserver_inspect',
            ),
            path(
                '<int:server_id>/inspect/<int:db>/<int:cursor>/<int:count>/key/<path:key>/',
                wrap(self.inspect_key),
                name='redisboard_redisserver_inspect',
            ),
        ] + urlpatterns

    def inspect_key(self, request, server: RedisServer, db: int, key: str, cursor: int = 0, count: int = 0):
        key: bytes = unquote_to_bytes(key)
        display = server.display
        if not server.connection.exists(key):
            return HttpResponseNotFound('Key is gone.')
        stats = display.keys(db, [key])
        scan = display.value(db, key, cursor=cursor, count=count)
        return render(
            request,
            'redisboard/inspect_key.html',
            {
                'key': display.decoder.key(key),
                'encoded_key': quote(key),
                'stats': stats,
                'count': scan.count + count,
                'scan': scan,
                'db': {
                    'id': db,
                    'cursor': cursor,
                },
                'original': server,
                'site_header': None,
                'opts': {'app_label': 'redisboard', 'object_name': 'redisserver'},
                'media': '',
            },
        )

    def inspect(self, request, server: RedisServer, db: Union[int, None] = None, cursor: Union[int, None] = 0):
        stats = server.stats
        active = None
        databases = []
        if stats:
            total_keys = sum(details.get('keys', 0) for details in stats.databases.values())
            if db is not None:
                if db in stats.databases:
                    active = DBInfo(db, stats.databases[db], scan=True)
                else:
                    active = DBInfo(db, {'keys': 0}, scan=True)
                databases = [active]
            elif total_keys < REDISBOARD_SCAN_COUNT:
                databases = [DBInfo(*item, scan=True) for item in stats.databases.items()]
            else:
                databases = [DBInfo(*item) for item in stats.databases.items()]

            for dbinfo in databases:
                if dbinfo.scan:
                    dbinfo.scan = server.display.scan(dbinfo.id, cursor=cursor, **request.GET.dict())
                    dbinfo.cursor = cursor
        return render(
            request,
            'redisboard/inspect.html',
            {
                'databases': databases,
                'display': server.display,
                'original': server,
                'stats': stats,
                'active': active,
                'filters': f'?{request.GET.urlencode()}' if request.GET else '',
                'site_header': None,
                'opts': {'app_label': 'redisboard', 'object_name': 'redisserver'},
                'media': '',
            },
        )


admin.site.register(RedisServer, RedisServerAdmin)
