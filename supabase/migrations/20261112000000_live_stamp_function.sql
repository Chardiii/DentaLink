-- Live updates: one round trip instead of up to 11.
--
-- Pages poll /api/live every 10 seconds. The app used to build the "has anything changed?"
-- fingerprint from up to 11 separate queries, each a ~200 ms round trip from the Philippines
-- to this Sydney project. This function computes the same fingerprint in one call.
--
-- The fingerprint only needs to change when something the user's pages show changes; pages
-- compare it with the previous poll, so its exact format doesn't matter.

create or replace function public.live_stamp(p_user uuid, p_role text)
returns jsonb
language sql
stable
security invoker
set search_path = public
as $$
  select jsonb_build_object(
    'stamp', concat_ws('|',
      coalesce((select max(created_at)::text from notifications where user_id = p_user), ''),
      coalesce((select max(updated_at)::text || ':' || count(*)::text
                from dental_cases
                where (p_role <> 'technician' or technician_id = p_user)
                  and (p_role <> 'dentist' or dentist_id = p_user)), ''),
      case when p_role = 'admin' then concat_ws('|',
        coalesce((select max(created_at)::text from inventory_logs), ''),
        (select count(*)::text from inventory),
        (select count(*)::text from material_requests where status = 'pending'),
        coalesce((select max(created_at)::text from profiles), ''),
        (select count(*)::text from profiles where status = 'pending'),
        (select count(*)::text from case_issue_reports where status = 'open'),
        coalesce((select id::text || ':' || status from forecast_runs order by created_at desc limit 1), '')
      ) end
    ),
    'latest', (select jsonb_build_object('id', id, 'message', message, 'created_at', created_at)
               from notifications where user_id = p_user
               order by created_at desc limit 1)
  );
$$;

-- Only the server (service role) calls this; it's not part of the public API.
revoke execute on function public.live_stamp(uuid, text) from public, anon, authenticated;
grant execute on function public.live_stamp(uuid, text) to service_role;
