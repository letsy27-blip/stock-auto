-- HONG STOCK 공개 앱용 개인 모의투자 DB
-- Supabase Dashboard > SQL Editor에서 한 번 실행합니다.

create table if not exists public.user_profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    nickname text not null default '',
    analytics_consent boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.paper_accounts (
    user_id uuid primary key references auth.users(id) on delete cascade,
    cash numeric not null default 100000000 check (cash >= 0),
    initial_cash numeric not null default 100000000 check (initial_cash > 0),
    updated_at timestamptz not null default now()
);

create table if not exists public.paper_positions (
    user_id uuid not null references auth.users(id) on delete cascade,
    stock_code text not null,
    stock_name text not null,
    quantity integer not null check (quantity >= 0),
    average_price numeric not null check (average_price >= 0),
    updated_at timestamptz not null default now(),
    primary key (user_id, stock_code)
);

create table if not exists public.paper_orders (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    ordered_at timestamptz not null default now(),
    side text not null check (side in ('BUY', 'SELL')),
    stock_code text not null,
    stock_name text not null,
    quantity integer not null check (quantity > 0),
    price numeric not null check (price > 0),
    fee numeric not null default 0,
    tax numeric not null default 0,
    amount numeric not null,
    realized_profit numeric not null default 0
);

create table if not exists public.investor_behavior_events (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    occurred_at timestamptz not null default now(),
    event_type text not null check (event_type in ('search', 'view', 'BUY', 'SELL')),
    stock_code text,
    stock_name text,
    metadata_json jsonb not null default '{}'::jsonb
);

create index if not exists idx_paper_orders_user_time
    on public.paper_orders(user_id, ordered_at desc);
create index if not exists idx_behavior_events_user_time
    on public.investor_behavior_events(user_id, occurred_at desc);

alter table public.user_profiles enable row level security;
alter table public.paper_accounts enable row level security;
alter table public.paper_positions enable row level security;
alter table public.paper_orders enable row level security;
alter table public.investor_behavior_events enable row level security;

create policy "profile owner only" on public.user_profiles
    for all to authenticated using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
create policy "account owner only" on public.paper_accounts
    for all to authenticated using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
create policy "position owner only" on public.paper_positions
    for all to authenticated using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
create policy "order owner only" on public.paper_orders
    for all to authenticated using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);
create policy "behavior owner only" on public.investor_behavior_events
    for all to authenticated using ((select auth.uid()) = user_id)
    with check ((select auth.uid()) = user_id);

create or replace function public.handle_new_hongstock_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    insert into public.user_profiles (user_id, nickname, analytics_consent)
    values (
        new.id,
        coalesce(new.raw_user_meta_data ->> 'nickname', ''),
        coalesce((new.raw_user_meta_data ->> 'analytics_consent')::boolean, false)
    ) on conflict (user_id) do nothing;
    insert into public.paper_accounts (user_id)
    values (new.id) on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_hongstock_auth_user_created on auth.users;
create trigger on_hongstock_auth_user_created
    after insert on auth.users
    for each row execute procedure public.handle_new_hongstock_user();

create or replace function public.paper_initialize_account()
returns jsonb
language plpgsql
security definer set search_path = public
as $$
declare v_user_id uuid := auth.uid();
begin
    if v_user_id is null then raise exception '로그인이 필요합니다.'; end if;
    insert into public.paper_accounts (user_id)
    values (v_user_id) on conflict (user_id) do nothing;
    return jsonb_build_object('ok', true);
end;
$$;

create or replace function public.paper_place_order(
    p_side text,
    p_stock_code text,
    p_stock_name text,
    p_quantity integer,
    p_price numeric
)
returns jsonb
language plpgsql
security definer set search_path = public
as $$
declare
    v_user_id uuid := auth.uid();
    v_cash numeric;
    v_held integer := 0;
    v_average numeric := 0;
    v_gross numeric;
    v_fee numeric := 0;
    v_tax numeric := 0;
    v_amount numeric;
    v_new_quantity integer;
    v_new_average numeric;
    v_realized numeric := 0;
begin
    if v_user_id is null then raise exception '로그인이 필요합니다.'; end if;
    if p_side not in ('BUY', 'SELL') or p_quantity <= 0 or p_price <= 0 then
        raise exception '주문 정보가 올바르지 않습니다.';
    end if;

    insert into public.paper_accounts (user_id) values (v_user_id)
    on conflict (user_id) do nothing;
    select cash into v_cash from public.paper_accounts where user_id = v_user_id for update;
    select quantity, average_price into v_held, v_average
    from public.paper_positions where user_id = v_user_id and stock_code = p_stock_code for update;
    v_held := coalesce(v_held, 0);
    v_average := coalesce(v_average, 0);
    v_gross := p_quantity * p_price;

    if p_side = 'BUY' then
        v_fee := round(v_gross * 0.00015);
        v_amount := v_gross + v_fee;
        if v_cash < v_amount then raise exception '주문 가능 금액이 부족합니다.'; end if;
        v_new_quantity := v_held + p_quantity;
        v_new_average := ((v_held * v_average) + v_gross + v_fee) / v_new_quantity;
        update public.paper_accounts set cash = v_cash - v_amount, updated_at = now() where user_id = v_user_id;
    else
        if v_held < p_quantity then raise exception '보유 수량보다 많이 매도할 수 없습니다.'; end if;
        v_fee := round(v_gross * 0.00015);
        v_tax := round(v_gross * 0.0018);
        v_amount := v_gross - v_fee - v_tax;
        v_new_quantity := v_held - p_quantity;
        v_new_average := case when v_new_quantity = 0 then 0 else v_average end;
        v_realized := v_amount - (v_average * p_quantity);
        update public.paper_accounts set cash = v_cash + v_amount, updated_at = now() where user_id = v_user_id;
    end if;

    if v_new_quantity > 0 then
        insert into public.paper_positions (user_id, stock_code, stock_name, quantity, average_price, updated_at)
        values (v_user_id, p_stock_code, p_stock_name, v_new_quantity, v_new_average, now())
        on conflict (user_id, stock_code) do update set stock_name = excluded.stock_name,
            quantity = excluded.quantity, average_price = excluded.average_price, updated_at = excluded.updated_at;
    else
        delete from public.paper_positions where user_id = v_user_id and stock_code = p_stock_code;
    end if;

    insert into public.paper_orders (user_id, side, stock_code, stock_name, quantity, price, fee, tax, amount, realized_profit)
    values (v_user_id, p_side, p_stock_code, p_stock_name, p_quantity, p_price, v_fee, v_tax, v_amount, v_realized);
    insert into public.investor_behavior_events (user_id, event_type, stock_code, stock_name, metadata_json)
    values (v_user_id, p_side, p_stock_code, p_stock_name, jsonb_build_object('quantity', p_quantity, 'price', p_price));

    return jsonb_build_object('side', p_side, 'amount', v_amount, 'fee', v_fee, 'tax', v_tax);
end;
$$;

create or replace function public.paper_reset_account()
returns jsonb
language plpgsql
security definer set search_path = public
as $$
declare v_user_id uuid := auth.uid();
begin
    if v_user_id is null then raise exception '로그인이 필요합니다.'; end if;
    delete from public.paper_orders where user_id = v_user_id;
    delete from public.paper_positions where user_id = v_user_id;
    update public.paper_accounts set cash = 100000000, initial_cash = 100000000, updated_at = now()
    where user_id = v_user_id;
    return jsonb_build_object('ok', true);
end;
$$;

grant usage on schema public to authenticated;
grant select, insert, update, delete on public.user_profiles, public.paper_accounts,
    public.paper_positions, public.paper_orders, public.investor_behavior_events to authenticated;
grant execute on function public.paper_initialize_account() to authenticated;
grant execute on function public.paper_place_order(text, text, text, integer, numeric) to authenticated;
grant execute on function public.paper_reset_account() to authenticated;

-- 분석용 집계는 이 원본 개인 테이블을 화면에 공개하지 않고,
-- analytics_consent=true 인 사용자만 대상으로 별도 배치 작업에서 생성합니다.
