from __future__ import annotations
import json, re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID
from .models import *

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 65536


class BrokerOperation(
    StrEnum): GET_STATUS = "get_status"; GET_PROFILES = "get_profiles"; APPLY_PROFILE = "apply_profile"; SUSPEND = "suspend"; RELEASE_SUSPEND = "release_suspend"; ACQUIRE_LEASE = "acquire_lease"; RENEW_LEASE = "renew_lease"; RELEASE_LEASE = "release_lease"; LIST_LEASES = "list_leases"


class BrokerFailureCode(StrEnum): VALIDATION = "validation"; OPERATIONAL = "operational"


@dataclass(frozen=True)
class BrokerRequest: request_id: UUID; operation: BrokerOperation = BrokerOperation.GET_STATUS; profile: str | None = None; principal_id: str | None = None; lease_id: str | None = None; requested_ttl_seconds: int | None = None; suspend_receipt_id: UUID | None = None


@dataclass(frozen=True)
class BrokerStatusResult: request_id: UUID; status: ServiceStatus


@dataclass(frozen=True)
class BrokerProfilesResult: request_id: UUID; names: tuple[
    str, ...]; reconciliation: ProfileReconciliation; unavailable: bool = False; error: ProfileErrorCode | None = None


@dataclass(frozen=True)
class BrokerProfileApplyResult: request_id: UUID; application: ProfileApplyResult; status: ServiceStatus


@dataclass(frozen=True)
class BrokerSuspendResult: request_id: UUID; suspend: SuspendResult; status: ServiceStatus; receipt_id: UUID | None = None


@dataclass(frozen=True)
class BrokerReleaseSuspendResult: request_id: UUID


@dataclass(frozen=True)
class BrokerLeaseResult: request_id: UUID; lease: Lease | None; error: LeaseError | None; status: ServiceStatus


@dataclass(frozen=True)
class BrokerLeasesResult: request_id: UUID; leases: tuple[Lease, ...]


@dataclass(frozen=True)
class BrokerFailure: request_id: UUID; code: BrokerFailureCode


_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")


def _unique(pairs):
    d = {}
    for k, v in pairs:
        if k in d: raise ValueError("duplicate field")
        d[k] = v
    return d


def _obj(frame):
    if isinstance(frame, bytes):
        if len(frame) > MAX_FRAME_BYTES: raise ValueError("oversize frame")
        frame = frame.decode()
    v = json.loads(frame, object_pairs_hook=_unique)
    if not isinstance(v, dict): raise ValueError("object required")
    return v


def _keys(d, keys):
    if not isinstance(d, dict) or set(d) != set(keys): raise ValueError("invalid fields")


def _uuid(v):
    if not isinstance(v, str) or str(UUID(v)) != v: raise ValueError("noncanonical uuid")
    return UUID(v)


def _id(v):
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v): raise ValueError("invalid profile")
    return v


def _timestamp(v):
    if v.tzinfo is None or v.utcoffset() != timezone.utc.utcoffset(None): raise ValueError("UTC required")
    return v.isoformat(timespec="microseconds" if v.microsecond else "seconds").replace("+00:00", "Z")


def _decimal(v): return format(v.normalize(), 'f') if v else '0'


def _status(s):
    def component(c, name):
        if c.availability is Availability.UNAVAILABLE: return {'state': 'unavailable', 'error': {'code': c.error.value}}
        if name == 'cpu':
            return {'state': 'available', 'policies': [
                {'policy': p.policy, 'affected_cpus': list(p.affected_cpus), 'driver': p.driver, 'governor': p.governor,
                 'available_governors': list(p.available_governors), 'hardware_min_khz': p.hardware_min_khz,
                 'hardware_max_khz': p.hardware_max_khz, 'configured_min_khz': p.configured_min_khz,
                 'configured_max_khz': p.configured_max_khz,
                 **({'epp': p.epp, 'available_epps': list(p.available_epps)} if p.epp is not None else {})} for p in
                c.value.policies]}
        if name == 'gpu':
            x = c.value
            return {'state': 'available', 'index': x.index, 'name': x.name, 'driver_version': x.driver_version,
                    'power_limit_w': _decimal(x.power_limit_w), 'power_limit_min_w': _decimal(x.power_limit_min_w),
                    'power_limit_max_w': _decimal(x.power_limit_max_w)}
        return {'state': 'available', 'can_suspend': c.value.can_suspend.value,
                'inhibitors': [{'what': i.what.value, 'mode': i.mode.value} for i in c.value.inhibitors]}

    p = s.profiles or ProfileReconciliation(ReconciliationState.UNMATCHED)
    pd = {'state': p.state.value, **({'name': p.name} if p.name else {}),
          **({'error': {'code': p.error.value}} if p.error else {})}
    from .mapping import status_to_dict
    lifecycle = {'lifecycle': status_to_dict(s)['lifecycle']} if s.lifecycle is not None else {}
    return {'observed_at': _timestamp(s.observed_at), 'service_state': s.state.value, 'cpu': component(s.cpu, 'cpu'),
            'gpu': component(s.gpu, 'gpu'), 'logind': component(s.logind, 'logind'),
            'profiles': {'state': 'available', 'names': list(s.profile_names), 'reconciliation': pd}, **lifecycle}


def _app(a):
    def cpu(c): return {'state': c.state.value, 'policies': [
        {'policy': p.policy, 'state': p.state.value, **({'error': {'code': p.error.value}} if p.error else {})} for p in
        c.policies]}

    def gpu(c): return {'state': c.state.value, 'index': c.index,
                        **({'error': {'code': c.error.value}} if c.error else {})}

    return {'profile': a.name, 'outcome': a.outcome.value,
            **({'rejection': {'code': a.rejection.value}} if a.rejection else {}), 'cpu': cpu(a.cpu), 'gpu': gpu(a.gpu)}


def _suspend(s):
    d = {'outcome': s.outcome.value}
    if s.can_suspend is not None: d['can_suspend'] = s.can_suspend.value
    if s.blockers: d['blockers'] = [{'what': x.what.value, 'mode': x.mode.value} for x in s.blockers]
    return d


def _lease(lease):
    return {'id': lease.lease_id, 'expires_at': _timestamp(lease.expires_at), 'ttl_seconds': lease.ttl_seconds}


def _encode_result(r):
    if isinstance(r, BrokerFailure): return {'version': 1, 'request_id': str(r.request_id), 'kind': 'failure',
                                             'code': r.code.value}
    if isinstance(r, BrokerStatusResult):
        value = _status(r.status)
        if r.status.profiles is None: value.pop('profiles', None)
        return {'version': 1, 'request_id': str(r.request_id), 'kind': 'status', 'status': value}
    if isinstance(r, BrokerProfilesResult):
        rec = {'state': r.reconciliation.state.value,
               **({'name': r.reconciliation.name} if r.reconciliation.name else {}),
               **({'error': {'code': r.reconciliation.error.value}} if r.reconciliation.error else {})}
        p = {'state': 'unavailable', 'names': list(r.names),
             'error': {'code': (r.error or ProfileErrorCode.CONFIGURATION).value},
             'reconciliation': {'state': 'unavailable', 'error': {
                 'code': (r.error or ProfileErrorCode.CONFIGURATION).value}}} if r.unavailable else {
            'state': 'available', 'names': list(r.names), 'reconciliation': rec}
        return {'version': 1, 'request_id': str(r.request_id), 'kind': 'profiles', 'profiles': p}
    if isinstance(r, BrokerSuspendResult):
        status = _status(r.status)
        if r.status.profiles is None: status.pop('profiles', None)
        return {'version': 1, 'request_id': str(r.request_id), 'kind': 'suspend', 'suspend': _suspend(r.suspend),
                'status': status, **({'receipt_id': str(r.receipt_id)} if r.receipt_id else {})}
    if isinstance(r, BrokerReleaseSuspendResult): return {'version': 1, 'request_id': str(r.request_id),
                                                          'kind': 'release_suspend'}
    if isinstance(r, BrokerLeaseResult):
        payload = {'lease': _lease(r.lease)} if r.lease else {'error': r.error.value} if r.error else {'released': True}
        status = _status(r.status)
        if r.status.profiles is None: status.pop('profiles', None)
        return {'version': 1, 'request_id': str(r.request_id), 'kind': 'lease', **payload, 'status': status}
    if isinstance(r, BrokerLeasesResult):
        return {'version': 1, 'request_id': str(r.request_id), 'kind': 'leases', 'leases': [_lease(lease) for lease in r.leases]}
    return {'version': 1, 'request_id': str(r.request_id), 'kind': 'profile_apply', 'application': _app(r.application),
            'status': _status(r.status)}


def encode_request(r):
    d = {'version': 1, 'request_id': str(r.request_id), 'operation': r.operation.value}
    if r.operation is BrokerOperation.APPLY_PROFILE: d['profile'] = _id(r.profile)
    if r.operation is BrokerOperation.RELEASE_SUSPEND: d['suspend_receipt_id'] = str(_uuid(str(r.suspend_receipt_id)))
    if r.operation in (BrokerOperation.ACQUIRE_LEASE, BrokerOperation.RENEW_LEASE, BrokerOperation.RELEASE_LEASE): d[
        'principal_id'] = _id(r.principal_id)
    if r.operation in (BrokerOperation.RENEW_LEASE, BrokerOperation.RELEASE_LEASE): d['lease_id'] = str(
        _uuid(r.lease_id))
    if r.operation in (BrokerOperation.ACQUIRE_LEASE, BrokerOperation.RENEW_LEASE):
        if not isinstance(r.requested_ttl_seconds,
                          int) or not 1 <= r.requested_ttl_seconds <= 2147483647: raise ValueError('invalid ttl')
        d['requested_ttl_seconds'] = r.requested_ttl_seconds
    return (json.dumps(d, separators=(',', ':')) + '\n').encode()


def decode_request(frame):
    d = _obj(frame)
    op = BrokerOperation(d.get('operation'))
    keys = ['version', 'request_id', 'operation'] + (['profile'] if op is BrokerOperation.APPLY_PROFILE else []) + (
        ['suspend_receipt_id'] if op is BrokerOperation.RELEASE_SUSPEND else []) + (
               ['principal_id'] if op in (BrokerOperation.ACQUIRE_LEASE, BrokerOperation.RENEW_LEASE,
                                          BrokerOperation.RELEASE_LEASE) else []) + (
               ['lease_id'] if op in (BrokerOperation.RENEW_LEASE, BrokerOperation.RELEASE_LEASE) else []) + (
               ['requested_ttl_seconds'] if op in (BrokerOperation.ACQUIRE_LEASE, BrokerOperation.RENEW_LEASE) else [])
    _keys(d, keys)
    if d['version'] != 1: raise ValueError('unsupported version')
    ttl = d.get('requested_ttl_seconds')
    if ttl is not None and (
            not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 2147483647): raise ValueError(
        'invalid ttl')
    return BrokerRequest(_uuid(d['request_id']), op, _id(d['profile']) if op is BrokerOperation.APPLY_PROFILE else None,
                         d.get('principal_id'), d.get('lease_id'), ttl,
                         _uuid(d['suspend_receipt_id']) if op is BrokerOperation.RELEASE_SUSPEND else None)


def encode_result(r): return (json.dumps(_encode_result(r), separators=(',', ':')) + '\n').encode()


def decode_result(frame):
    d = _obj(frame)
    _keys(d, ['version', 'request_id', 'kind'] + (
        ['code'] if d.get('kind') == 'failure' else [] if d.get('kind') == 'release_suspend' else ['leases'] if d.get('kind') == 'leases' else ['status'] if d.get(
            'kind') == 'status' else ['profiles'] if d.get('kind') == 'profiles' else ['suspend', 'status'] + (
            ['receipt_id'] if 'receipt_id' in d else []) if d.get('kind') == 'suspend' else ['lease',
                                                                                             'status'] if d.get(
            'kind') == 'lease' and 'lease' in d else ['error', 'status'] if d.get(
            'kind') == 'lease' and 'error' in d else ['released', 'status'] if d.get('kind') == 'lease' else [
            'application', 'status']))
    if d['version'] != 1: raise ValueError('unsupported version')
    rid = _uuid(d['request_id'])
    if d['kind'] == 'failure': return BrokerFailure(rid, BrokerFailureCode(d['code']))
    if d['kind'] == 'release_suspend': return BrokerReleaseSuspendResult(rid)
    if d['kind'] == 'leases':
        leases = []
        for x in d['leases']:
            _keys(x, ['id', 'expires_at', 'ttl_seconds'])
            leases.append(Lease(str(_uuid(x['id'])), "", x['ttl_seconds'], datetime.fromisoformat(x['expires_at'].replace('Z', '+00:00'))))
        return BrokerLeasesResult(rid, tuple(leases))
    if d['kind'] == 'lease':
        if 'error' in d: return BrokerLeaseResult(rid, None, LeaseError(d['error']), status_from_dict(d['status']))
        if d.get('released') is True: return BrokerLeaseResult(rid, None, None, status_from_dict(d['status']))
        x = d['lease']
        _keys(x, ['id', 'expires_at', 'ttl_seconds'])
        return BrokerLeaseResult(rid, Lease(str(_uuid(x['id'])), "", x['ttl_seconds'],
                                            datetime.fromisoformat(x['expires_at'].replace('Z', '+00:00'))), None,
                                 status_from_dict(d['status']))
    # The broker client uses the strict envelope checks and reconstructs domain data through mapping.
    if d['kind'] == 'status': return BrokerStatusResult(rid, status_from_dict(d['status']))
    if d['kind'] == 'profiles':
        p = d['profiles']
        _keys(p, ['state', 'names', 'reconciliation'] if p.get('state') == 'available' else ['state', 'names', 'error',
                                                                                             'reconciliation'])
        names = tuple(_id(x) for x in p['names'])
        rec = p['reconciliation']
        state = ReconciliationState(rec['state'])
        return BrokerProfilesResult(rid, names, ProfileReconciliation(state, rec.get('name'),
                                                                      ProfileErrorCode(rec['error']['code']) if rec.get(
                                                                          'error') else None),
                                    p['state'] == 'unavailable',
                                    ProfileErrorCode(p['error']['code']) if p.get('error') else None)
    if d['kind'] == 'suspend':
        s = d['suspend']
        outcome = SuspendOutcome(s.get('outcome'))
        keys = ['outcome'] + (
            ['can_suspend'] if outcome not in (SuspendOutcome.CONFLICT, SuspendOutcome.OPERATIONAL_FAILURE) else []) + (
                   ['blockers'] if outcome is SuspendOutcome.BLOCKED else [])
        _keys(s, keys)
        blockers = tuple(
            SuspendBlocker(InhibitorKind(x['what']), InhibitorMode(x['mode'])) for x in s.get('blockers', []))
        receipt_id = _uuid(d['receipt_id']) if 'receipt_id' in d else None
        if (outcome is SuspendOutcome.ACCEPTED) != (receipt_id is not None): raise ValueError('invalid suspend receipt')
        return BrokerSuspendResult(rid,
                                   SuspendResult(outcome, CanSuspend(s['can_suspend']) if 'can_suspend' in s else None,
                                                 blockers), status_from_dict(d['status']), receipt_id)
    a = d['application']
    _keys(a,
          ['profile', 'outcome', 'cpu', 'gpu'] if 'rejection' not in a else ['profile', 'outcome', 'rejection', 'cpu',
                                                                             'gpu'])

    def pc(x):
        _keys(x, ['state', 'policies'])
        ps = []
        for p in x['policies']:
            _keys(p, ['policy', 'state'] + (['error'] if 'error' in p else []))
            ps.append(PolicyApplicationResult(p['policy'], ProfileComponentState(p['state']),
                                              ProfileErrorCode(p['error']['code']) if p.get('error') else None))
        return CpuApplicationResult(ProfileComponentState(x['state']), tuple(ps))

    def pg(x):
        _keys(x, ['state', 'index'] + (['error'] if 'error' in x else []))
        return GpuApplicationResult(ProfileComponentState(x['state']), x['index'],
                                    ProfileErrorCode(x['error']['code']) if x.get('error') else None)

    app = ProfileApplyResult(_id(a['profile']), ProfileOutcome(a['outcome']), pc(a['cpu']), pg(a['gpu']),
                             ProfileErrorCode(a['rejection']['code']) if a.get('rejection') else None)
    return BrokerProfileApplyResult(rid, app, status_from_dict(d['status']))


def status_from_dict(s):
    from .mapping import status_from_dict as convert
    return convert(s)


class ProfileStatus:
    def __init__(self, p): self.profiles = p
