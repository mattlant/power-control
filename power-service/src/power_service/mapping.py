from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from .models import *


def status_to_dict(s):
    def comp(c, n):
        if c.availability is Availability.UNAVAILABLE: return {'state': 'unavailable', 'error': {'code': c.error.value}}
        if n == 'cpu': return {'state': 'available', 'policies': [
            {'policy': p.policy, 'affected_cpus': list(p.affected_cpus), 'driver': p.driver, 'governor': p.governor,
             'available_governors': list(p.available_governors), 'hardware_min_khz': p.hardware_min_khz,
             'hardware_max_khz': p.hardware_max_khz, 'configured_min_khz': p.configured_min_khz,
             'configured_max_khz': p.configured_max_khz,
             **({'epp': p.epp, 'available_epps': list(p.available_epps)} if p.epp is not None else {})} for p in
            c.value.policies]}
        if n == 'gpu':
            x = c.value
            return {'state': 'available', 'index': x.index, 'name': x.name, 'driver_version': x.driver_version,
                    'power_limit_w': format(x.power_limit_w.normalize(), 'f'),
                    'power_limit_min_w': format(x.power_limit_min_w.normalize(), 'f'),
                    'power_limit_max_w': format(x.power_limit_max_w.normalize(), 'f')}
        return {'state': 'available', 'can_suspend': c.value.can_suspend.value,
                'inhibitors': [{'what': i.what.value, 'mode': i.mode.value} for i in c.value.inhibitors]}

    p = s.profiles or ProfileReconciliation(ReconciliationState.UNMATCHED)
    result = {'observed_at': s.observed_at.isoformat(timespec='seconds').replace('+00:00', 'Z'),
              'service_state': s.state.value, 'cpu': comp(s.cpu, 'cpu'), 'gpu': comp(s.gpu, 'gpu'),
              'logind': comp(s.logind, 'logind')}
    if s.profiles is not None: result['profiles'] = {'state': 'available', 'names': list(s.profile_names),
                                                     'reconciliation': {'state': p.state.value,
                                                                        **({'name': p.name} if p.name else {}), **(
                                                             {'error': {'code': p.error.value}} if p.error else {})}}
    lifecycle = s.lifecycle or LifecycleStatus()
    result['lifecycle'] = {'state': lifecycle.state.value, 'blockers': [x.value for x in lifecycle.blockers],
                           'idle_started_at': lifecycle.idle_started_at.isoformat().replace('+00:00',
                                                                                            'Z') if lifecycle.idle_started_at else None,
                           'grace_started_at': lifecycle.grace_started_at.isoformat().replace('+00:00',
                                                                                              'Z') if lifecycle.grace_started_at else None,
                           'next_transition_at': lifecycle.next_transition_at.isoformat().replace('+00:00',
                                                                                                  'Z') if lifecycle.next_transition_at else None,
                           'leases': {'active_count': lifecycle.leases.active_count,
                                      'active_principal_count': lifecycle.leases.active_principal_count,
                                      'principal_ids': list(lifecycle.leases.principal_ids),
                                      'principals_truncated': lifecycle.leases.principals_truncated},
                           'last_result': None if lifecycle.last_result is None else {
                               'outcome': lifecycle.last_result.outcome.value,
                               'occurred_at': lifecycle.last_result.occurred_at.isoformat().replace('+00:00', 'Z')}}
    return result


def status_from_dict(s):
    def comp(d, n):
        if d['state'] == 'unavailable': return ComponentStatus(Availability.UNAVAILABLE,
                                                               error=OperationalErrorCode(d['error']['code']))
        if n == 'cpu':
            ps = []
            for p in d['policies']: ps.append(
                CpuPolicyStatus(p['policy'], tuple(p['affected_cpus']), p['driver'], p['governor'],
                                tuple(p['available_governors']), p['hardware_min_khz'], p['hardware_max_khz'],
                                p['configured_min_khz'], p['configured_max_khz'], p.get('epp'),
                                tuple(p['available_epps']) if 'available_epps' in p else None))
            return ComponentStatus(Availability.AVAILABLE, CpuStatus(tuple(ps)))
        if n == 'gpu': return ComponentStatus(Availability.AVAILABLE,
                                              GpuStatus(d['index'], d['name'], d['driver_version'],
                                                        Decimal(d['power_limit_w']), Decimal(d['power_limit_min_w']),
                                                        Decimal(d['power_limit_max_w'])))
        return ComponentStatus(Availability.AVAILABLE, LogindStatus(CanSuspend(d['can_suspend']), tuple(
            InhibitorStatus(InhibitorKind(x['what']), InhibitorMode(x['mode'])) for x in d['inhibitors'])))

    raw = s.get('profiles')
    p = None if raw is None else ProfileReconciliation(ReconciliationState(raw['reconciliation']['state']),
                                                       raw['reconciliation'].get('name'),
                                                       ProfileErrorCode(raw['reconciliation']['error']['code']) if raw[
                                                           'reconciliation'].get('error') else None)
    lifecycle_raw = s.get('lifecycle')
    lifecycle = None
    if lifecycle_raw:
        leases = lifecycle_raw['leases']
        parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None
        last_raw = lifecycle_raw.get('last_result')
        last_result = None if last_raw is None else AutomaticSuspendResult(SuspendOutcome(last_raw['outcome']),
                                                                           parse(last_raw['occurred_at']))
        lifecycle = LifecycleStatus(LifecycleState(lifecycle_raw['state']),
                                    tuple(LifecycleBlocker(x) for x in lifecycle_raw['blockers']),
                                    parse(lifecycle_raw['idle_started_at']), parse(lifecycle_raw['grace_started_at']),
                                    parse(lifecycle_raw['next_transition_at']),
                                    LeaseSummary(leases['active_count'], leases['active_principal_count'],
                                                 tuple(leases['principal_ids']), leases['principals_truncated']),
                                    last_result)
    return ServiceStatus(datetime.fromisoformat(s['observed_at'].replace('Z', '+00:00')),
                         ServiceState(s['service_state']), comp(s['cpu'], 'cpu'), comp(s['gpu'], 'gpu'),
                         comp(s['logind'], 'logind'), p, tuple(raw.get('names', ())) if raw else (), lifecycle)


def service_status_to_status_dto(s):
    d = status_to_dict(s)
    d.pop('observed_at', None)
    d['service'] = {'state': d.pop('service_state')}
    return d


def service_status_to_broker_result(status, request_id):
    from .protocol import BrokerStatusResult
    return BrokerStatusResult(request_id, status)


def broker_result_to_service_status(result):
    return result.status
