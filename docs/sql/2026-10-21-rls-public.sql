-- 2026-10-21 — RLS su TUTTE le tabelle dello schema public
-- Risponde all'alert Supabase «CRITICAL: Table publicly accessible
-- (rls_disabled_in_public)»: una tabella nuova (recovery_action_invoices)
-- era stata creata dal backend senza RLS perché la lista in _enable_rls era
-- scritta a mano. Il backend ora deriva la lista dai modelli; questa SQL
-- chiude il buco subito, senza aspettare il deploy.
--
-- Idempotente: si può rieseguire. Eseguire nel SQL editor di Supabase
-- (progetto sc-recupero-crediti) come ruolo postgres (default dell'editor).
-- Il backend si collega come postgres (owner delle tabelle, policy
-- backend_full_access) e NON è toccato. L'app non usa mai la chiave anon.

-- 1) RLS + policy per il backend su ogni tabella public (anche legacy)
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('alter table public.%I enable row level security', t.tablename);
    execute format('drop policy if exists backend_full_access on public.%I', t.tablename);
    execute format(
      'create policy backend_full_access on public.%I for all to postgres using (true) with check (true)',
      t.tablename
    );
  end loop;
end $$;

-- 2) Seconda cintura: i ruoli della Data API (anon, authenticated) non hanno
--    più alcun privilegio sulle tabelle public, presenti e future.
revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

-- 3) Verifica: ogni riga deve avere rowsecurity = true
select tablename, rowsecurity
from pg_tables
where schemaname = 'public'
order by tablename;
