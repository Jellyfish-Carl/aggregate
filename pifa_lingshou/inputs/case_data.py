"""Dated forecasts for signing, delivery windows and realized-data entry."""
from __future__ import annotations
import calendar
from copy import deepcopy
from datetime import date, timedelta
from math import isfinite, sin, pi
from random import Random
from ..config.cases import defaults
from ..data_objects.model import Customer

NODES=('ANNUAL','MONTHLY','TEN_DAY','D-3','D-2','L3-A','STORAGE-DA','STORAGE-RT')


def curve(values,name,nonnegative=False,length=96):
    if not isinstance(values,list) or len(values)!=length:
        raise ValueError(f'{name}须为{length}个数值')
    if any(not isinstance(v,(float,int)) or not isfinite(v) or (nonnegative and v<0) for v in values):
        raise ValueError(name+'须为有限'+('非负' if nonnegative else '')+'数值')
    return values


def dates(start,end):
    start=date.fromisoformat(start);end=date.fromisoformat(end)
    if not 0<=(end-start).days<=365: raise ValueError('预测范围须为1至366天')
    return [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]


def windows(target):
    d=date.fromisoformat(target);month_end=calendar.monthrange(d.year,d.month)[1]
    month_start=d.replace(day=1);ten_start=d.replace(day=min((d.day-1)//10*10+1,21))
    ten_end=d.replace(day=min(ten_start.day+9,month_end)) if d.day<=20 else d.replace(day=month_end)
    result={
        'ANNUAL':(date(d.year-1,12,1),date(d.year,1,1),date(d.year,12,31)),
        'MONTHLY':(month_start-timedelta(days=5),month_start,d.replace(day=month_end)),
        'TEN_DAY':(min(ten_start-timedelta(days=1),d-timedelta(days=4)),ten_start,ten_end),
    }
    for n,days_back in (('D-3',3),('D-2',2),('L3-A',1),('STORAGE-DA',1),('STORAGE-RT',0)):
        result[n]=(d-timedelta(days=days_back),d,d)
    return {k:dict(as_of=a.isoformat(),scope_start=s.isoformat(),scope_end=e.isoformat()) for k,(a,s,e) in result.items()}


def mock_case(settings=None):
    cfg=defaults(); supplied=settings or {}
    cfg.update({k:v for k,v in supplied.items() if k not in ('market','storage','recommendation_policy')})
    for key in ('market','storage','recommendation_policy'):
        cfg[key]={**defaults()[key],**supplied.get(key,{})}
    count=cfg['customer_count'];days=cfg['recommendation_days']
    if type(count)!=int or not 1<=count<=20: raise ValueError('客户数量须为1至20')
    if type(days)!=int or not 7<=days<=366: raise ValueError('签约预测期须为7至366天')
    rng=Random(cfg['seed']); target=date.fromisoformat(cfg['target_date'])
    rt=[float(260 if 40<=j<60 else 610 if 68<=j<84 else 405) for j in range(96)]
    da=[p*.97+7 for p in rt]
    tou=[float(310 if j<28 else 730 if 36<=j<44 or 68<=j<84 else 510) for j in range(96)]
    customers=[]
    for i in range(count):
        uncertainty=(.06,.18,.36)[i%3]
        profile=[round((.6 if j<28 else 1.7 if 32<=j<72 else 1.)*(1+.09*i),6) for j in range(96)]
        terms=Customer(f'C{i+1}',[455.+i*3]*96,[22.+i]*96,432.+i*2,.5,.5)
        from dataclasses import asdict
        customers.append(dict(terms=asdict(terms),load_profile_mwh=profile,
            daily_variation=uncertainty/2,forecast_uncertainty=uncertainty,tou_price=tou.copy()))
    cfg.update(customers=customers)
    cfg['market'].setdefault('day_ahead_price',da)
    cfg['market'].setdefault('real_time_price',rt)
    da=cfg['market']['day_ahead_price'];rt=cfg['market']['real_time_price']
    start=target-timedelta(days=min(target.day-1,days-1))
    cfg['signing_forecast']=dict(issued_at=date(target.year-1,11,30).isoformat(),
        start_date=start.isoformat(),end_date=(start+timedelta(days=days-1)).isoformat(),days=[])
    for di,day in enumerate(dates(cfg['signing_forecast']['start_date'],cfg['signing_forecast']['end_date'])):
        cfg['signing_forecast']['days'].append(dict(date=day,
            load_factors={c['terms']['customer_id']:round(max(.15,1+rng.uniform(-1,1)*c['daily_variation']*2),6) for c in customers},
            price_factor=round(1+.07*sin(di*pi/7),6)))
    cfg['forecasts']={}
    for ni,(node,window) in enumerate(windows(cfg['target_date']).items()):
        shrink=(1,.8,.65,.5,.35,.2,.2,.15)[ni]
        loads={c['terms']['customer_id']:[round(v*(1+.025*sin(ni+i)),6) for v in c['load_profile_mwh']] for i,c in enumerate(customers)}
        low={c['terms']['customer_id']:[v*(1-c['forecast_uncertainty']*shrink) for v in loads[c['terms']['customer_id']]] for c in customers}
        high={c['terms']['customer_id']:[v*(1+c['forecast_uncertainty']*shrink) for v in loads[c['terms']['customer_id']]] for c in customers}
        cfg['forecasts'][node]=dict(window,customer_load_mwh=loads,customer_p10_mwh=low,customer_p90_mwh=high,
            day_ahead_price=[p+12*(1-shrink) for p in da],real_time_price=[p+8*(1-shrink) for p in rt],
            price_uncertainty_ratio=cfg['market']['price_uncertainty_ratio']*shrink,
            annual_price=cfg['market']['annual_price'],monthly_price=cfg['market']['monthly_price'],ten_day_price=cfg['market']['ten_day_price'])
    cfg['source']='MOCK';validate_case(cfg)
    return cfg


def validate_case(data):
    target=date.fromisoformat(data['target_date']);customers=data['customers']
    if len(customers)!=data['customer_count'] or not 1<=len(customers)<=20: raise ValueError('客户数量与列表不一致')
    ids=[c['terms']['customer_id'] for c in customers]
    if len(set(ids))!=len(ids): raise ValueError('客户ID重复')
    for c in customers:
        Customer(**c['terms']).validate();curve(c['load_profile_mwh'],'客户负荷',True);curve(c['tou_price'],'分时基准电价')
        for k in ('daily_variation','forecast_uncertainty'):
            if not isinstance(c[k],(int,float)) or not isfinite(c[k]) or not 0<=c[k]<1: raise ValueError(k+'须在[0,1)')
    market=data['market']
    for k in ('annual_price','monthly_price','ten_day_price','annual_reference_price','monthly_reference_price','operating_cost_yuan_per_mwh','price_uncertainty_ratio'):
        if not isinstance(market[k],(float,int)) or not isfinite(market[k]): raise ValueError(k+'须为有限数值')
    if market['operating_cost_yuan_per_mwh']<0 or not 0<=market['price_uncertainty_ratio']<1: raise ValueError('运营费或价格不确定性无效')
    for k in ('reference_weights','procurement_weights'):
        curve(market[k],k,True,3)
        if abs(sum(market[k])-1)>1e-8: raise ValueError(k+'合计须为1')
    for k in ('day_ahead_price','real_time_price'):curve(market[k],k)
    sf=data['signing_forecast']; expected=dates(sf['start_date'],sf['end_date'])
    if len(expected)!=data['recommendation_days']:raise ValueError('推荐预测天数与逐日数据不一致')
    if [d['date'] for d in sf['days']]!=expected: raise ValueError('签约预测必须逐日覆盖整个范围')
    if date.fromisoformat(sf['issued_at'])>=date.fromisoformat(sf['start_date']): raise ValueError('签约预测须在预测范围开始前发布')
    for day in sf['days']:
        if set(day['load_factors'])!=set(ids): raise ValueError('逐日预测缺客户')
        curve(list(day['load_factors'].values()),'逐日负荷系数',True,len(ids))
        if not isfinite(day['price_factor']) or day['price_factor']<0: raise ValueError('逐日电价系数无效')
    policy=data['recommendation_policy']
    for k in ('minimum_total_profit_yuan','minimum_customer_saving_yuan'):
        if k not in policy or not isfinite(policy[k]) or policy[k]<0: raise ValueError(k+'必须为非负有限数')
    cvar_limit=policy.get('maximum_profit_loss_cvar_yuan')
    if cvar_limit is not None and (not isfinite(cvar_limit) or cvar_limit<0): raise ValueError('maximum_profit_loss_cvar_yuan必须为非负有限数或空值')
    if not 0<=data['risk_lambda']<=1 or type(data['scenario_count'])!=int or not 1<=data['scenario_count']<=100:
        raise ValueError('λ或场景数量无效')
    if set(data['forecasts'])!=set(NODES): raise ValueError('缺少分节点预测')
    last=date.fromisoformat(sf['issued_at'])
    for node in NODES:
        f=data['forecasts'][node];asof=date.fromisoformat(f['as_of'])
        days=dates(f['scope_start'],f['scope_end'])
        if not f['scope_start']<=target.isoformat()<=f['scope_end']: raise ValueError(node+'交割范围必须包含标的日')
        if asof<last or asof>target or (node!='STORAGE-RT' and asof>=target): raise ValueError(node+'决策日期顺序不合法')
        if asof>date.fromisoformat(days[0]): raise ValueError(node+'不能在交割开始后使用事前预测')
        last=asof
        for field in ('customer_load_mwh','customer_p10_mwh','customer_p90_mwh'):
            if set(f[field])!=set(ids): raise ValueError(node+'客户集合不匹配')
            for cid,v in f[field].items():curve(v,node+cid+field,True)
        for cid in ids:
            if any(not l<=m<=h for l,m,h in zip(f['customer_p10_mwh'][cid],f['customer_load_mwh'][cid],f['customer_p90_mwh'][cid])):
                raise ValueError(node+'负荷P10/P50/P90顺序不合法')
        for field in ('day_ahead_price','real_time_price'):curve(f[field],field)
        for field in ('annual_price','monthly_price','ten_day_price','price_uncertainty_ratio'):
            if not isfinite(f[field]):raise ValueError(field+'无效')
        if not 0<=f['price_uncertainty_ratio']<1:raise ValueError('价格不确定性无效')
    return data


def layer_input(data,node,packages):
    f=data['forecasts'][node];customers=data['customers'];ids=[c['terms']['customer_id'] for c in customers]
    load=[sum(f['customer_load_mwh'][cid][j] for cid in ids) for j in range(96)]
    shares=[[f['customer_load_mwh'][cid][j]/load[j] if load[j] else 1/len(ids) for j in range(96)] for cid in ids]
    result=dict(package='F',customer_packages=packages,customers=[c['terms'] for c in customers],customer_shares=shares,
        risk_lambda=data['risk_lambda'],scenario_count=data['scenario_count'],target_date=data['target_date'],as_of=f['as_of'],
        annual_reference_price=data['market']['annual_reference_price'],monthly_reference_price=data['market']['monthly_reference_price'],
        reference_weights=data['market']['reference_weights'],
        annual_price=f['annual_price'],monthly_price=f['monthly_price'],ten_day_price=f['ten_day_price'],
        load_forecast_mwh=load,load_p10_mwh=[sum(f['customer_p10_mwh'][cid][j] for cid in ids) for j in range(96)],
        load_p90_mwh=[sum(f['customer_p90_mwh'][cid][j] for cid in ids) for j in range(96)],
        day_ahead_price_forecast=f['day_ahead_price'],rt_price_forecast=f['real_time_price'],
        physical_peak_mw=max(20.,max(load)*4*1.3),storage=data['storage'],
        delivery_days={n:len(dates(data['forecasts'][n]['scope_start'],data['forecasts'][n]['scope_end'])) for n in NODES[:5]})
    for prefix,key in (('day_ahead_price','day_ahead_price'),('rt_price','real_time_price')):
        result[prefix+'_p10']=[p-abs(p)*f['price_uncertainty_ratio'] for p in f[key]]
        result[prefix+'_p90']=[p+abs(p)*f['price_uncertainty_ratio'] for p in f[key]]
    return result
