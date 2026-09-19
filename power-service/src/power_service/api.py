from __future__ import annotations

import asyncio
import re
import uuid
from aiohttp import web

from .auth import authorize
from .models import Permission, ProfileErrorCode, SuspendOutcome


class ApiApplication:
    @staticmethod
    def create(config, authenticator, broker_client):
        @web.middleware
        async def request_id(request, handler):
            request['request_id'] = str(uuid.uuid4())
            return await handler(request)

        async def principal(request, permission):
            p = authenticator.authenticate(request.headers.get('Authorization'))
            if not p: return None, web.json_response(
                {'error': {'code': 'unauthenticated', 'request_id': request['request_id']}}, status=401)
            if not authorize(p, permission): return None, web.json_response(
                {'error': {'code': 'forbidden', 'request_id': request['request_id']}}, status=403)
            return p, None

        async def status(request):
            p, e = await principal(request, Permission.STATUS)
            if e: return e
            try:
                s = await asyncio.wait_for(broker_client.get_status(),
                                           config.request_timeout_seconds)
                from .mapping import \
                    service_status_to_status_dto
                return web.json_response(
                    {'request_id': request['request_id']} | service_status_to_status_dto(s))
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}},
                                         status=503)

        async def profiles(request):
            p, e = await principal(request, Permission.PROFILE)
            if e: return e
            try:
                r = await asyncio.wait_for(broker_client.get_profiles(),
                                           config.request_timeout_seconds)
                return web.json_response(
                    {'request_id': request['request_id'], 'state': 'unavailable' if r.unavailable else 'available',
                     'names': list(r.names), 'reconciliation': {'state': r.reconciliation.state.value, **(
                        {'name': r.reconciliation.name} if r.reconciliation.name else {})}})
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}},
                                         status=503)

        async def apply(request):
            p, e = await principal(request, Permission.PROFILE)
            if e: return e
            name = request.match_info['name']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or await request.read(): return web.json_response(
                {'error': {'code': 'validation', 'request_id': request['request_id']}}, status=422)
            try:
                r = await asyncio.wait_for(broker_client.apply_profile(name), config.request_timeout_seconds)
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}},
                                         status=503)
            if r.application.rejection:
                code = 'profile_not_found' if r.application.rejection is ProfileErrorCode.PROFILE_NOT_FOUND else 'invalid_profile' if r.application.rejection in (
                    ProfileErrorCode.INVALID_PROFILE, ProfileErrorCode.CONFIGURATION) else r.application.rejection.value
                return web.json_response({'error': {'code': code, 'request_id': request['request_id']}},
                                         status=404 if code == 'profile_not_found' else 422)
            from .protocol import _status, _app
            return web.json_response(
                {'request_id': request['request_id'], 'application': _app(r.application), 'status': _status(r.status)})

        async def suspend(request):
            p, e = await principal(request, Permission.SUSPEND)
            if e: return e
            if request.query_string or request.content_length not in (None,
                                                                      0) or await request.read(): return web.json_response(
                {'error': {'code': 'validation', 'request_id': request['request_id']}}, status=422)
            try:
                r = await asyncio.wait_for(broker_client.suspend(), config.request_timeout_seconds)
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}},
                                         status=503)
            outcome = r.suspend.outcome
            if outcome is SuspendOutcome.ACCEPTED:
                from .mapping import service_status_to_status_dto
                response = web.json_response({'request_id': request['request_id'], 'suspend': {'outcome': outcome.value,
                                                                                               'can_suspend': r.suspend.can_suspend.value},
                                              'status': service_status_to_status_dto(r.status)}, status=202)
                released = False
                try:
                    await asyncio.wait_for(response.prepare(request), 1)
                    await asyncio.wait_for(response.write_eof(), 1)
                    await asyncio.wait_for(broker_client.release_suspend(r.receipt_id), 1)
                    released = True
                    return response
                finally:
                    if not released:
                        try:
                            await asyncio.wait_for(broker_client.release_suspend(r.receipt_id), 1)
                        except Exception:
                            pass
            errors = {SuspendOutcome.BLOCKED: ('suspend_blocked', 409),
                      SuspendOutcome.CONFLICT: ('suspend_conflict', 409),
                      SuspendOutcome.UNAVAILABLE: ('suspend_unavailable', 503),
                      SuspendOutcome.AUTHORIZATION_REQUIRED: ('suspend_authorization_required', 503),
                      SuspendOutcome.OPERATIONAL_FAILURE: ('suspend_operational_failure', 503)}
            code, http = errors[outcome]
            return web.json_response({'error': {'code': code, 'request_id': request['request_id']}}, status=http)

        async def lease(request, action):
            p, e = await principal(request, Permission.LEASE)
            if e: return e
            lease_id = request.match_info.get('lease_id')
            if request.query_string or (
                    lease_id and (not re.fullmatch(r'[0-9a-f-]{36}', lease_id))): return web.json_response(
                {'error': {'code': 'validation', 'request_id': request['request_id']}}, status=422)
            ttl = None
            if action != 'release':
                try:
                    body = await request.json()
                    if set(body) != {'ttl_seconds'} or not isinstance(body['ttl_seconds'], int) or isinstance(
                            body['ttl_seconds'], bool) or not 1 <= body['ttl_seconds'] <= 2147483647: raise ValueError()
                    ttl = body['ttl_seconds']
                except Exception:
                    return web.json_response({'error': {'code': 'validation', 'request_id': request['request_id']}},
                                             status=422)
            elif await request.read():
                return web.json_response({'error': {'code': 'validation', 'request_id': request['request_id']}},
                                         status=422)
            try:
                call = {'acquire': broker_client.acquire_lease, 'renew': broker_client.renew_lease,
                        'release': broker_client.release_lease}[action]
                args = (p.credential_id, ttl) if action == 'acquire' else (p.credential_id, lease_id,
                                                                           ttl) if action == 'renew' else (
                    p.credential_id, lease_id)
                result = await asyncio.wait_for(call(*args), config.request_timeout_seconds)
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}},
                                         status=503)
            if result.error: return web.json_response(
                {'error': {'code': 'lease_not_found', 'request_id': request['request_id']}}, status=404)
            if action == 'release': return web.Response(status=204)
            from .mapping import service_status_to_status_dto
            return web.json_response({'request_id': request['request_id'], 'lease': {'id': result.lease.lease_id,
                                                                                     'expires_at': result.lease.expires_at.isoformat().replace(
                                                                                         '+00:00', 'Z'),
                                                                                     'ttl_seconds': result.lease.ttl_seconds},
                                      'status': service_status_to_status_dto(result.status)}, status=201)

        async def list_leases(request):
            p, e = await principal(request, Permission.LEASE)
            if e: return e
            if request.query_string or request.content_length not in (None, 0) or await request.read(): return web.json_response(
                {'error': {'code': 'validation', 'request_id': request['request_id']}}, status=422)
            try:
                result = await asyncio.wait_for(broker_client.list_leases(), config.request_timeout_seconds)
            except Exception:
                return web.json_response({'error': {'code': 'broker_unavailable', 'request_id': request['request_id']}}, status=503)
            return web.json_response({'request_id': request['request_id'], 'leases': [
                {'id': lease.lease_id, 'expires_at': lease.expires_at.isoformat().replace('+00:00', 'Z'),
                 'ttl_seconds': lease.ttl_seconds} for lease in result.leases]})

        app = web.Application(middlewares=[request_id])
        app.router.add_get('/v1/status', status)
        app.router.add_get('/v1/profiles', profiles)
        app.router.add_post('/v1/profiles/{name}', apply)
        app.router.add_post('/v1/suspend', suspend)
        app.router.add_post('/v1/leases', lambda r: lease(r, 'acquire'))
        app.router.add_get('/v1/leases', list_leases)
        app.router.add_post('/v1/leases/{lease_id}/renew', lambda r: lease(r, 'renew'))
        app.router.add_delete('/v1/leases/{lease_id}', lambda r: lease(r, 'release'))
        return app
