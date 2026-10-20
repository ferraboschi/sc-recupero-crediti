-- 2026-10-21 — RLS su TUTTE le tabelle dello schema public
-- Risponde all'alert Supabase «CRITICAL: Table publicly accessible
-- (rls_disabled_in_public)» su recovery_action_invoices: la tabella era stata
-- creata dal backend senza RLS perché la lista in _enable_rls era scritta a
-- mano. Il backend ora deriva la lista dai modelli; questa SQL chiude il
-- buco subito, senza aspettare il deploy.
--
-- Idempotente: si può rieseguire. Eseguire nel SQL editor di Supabase
-- (progetto sc-recupero-crediti) come ruolo postgres (default dell'editor),
-- preferibilmente non mentre gira un sync (prende lock brevi sulle tabelle).
-- Il backend si collega come postgres (owner delle tabelle) e NON è toccato.
-- L'app non usa mai la chiave anon né la Data API.

set lock_timeout = '5s';

-- 1) RLS + policy per il backend su ogni tabella public (anche legacy).
--    Un errore su una tabella viene segnalato (NOTICE/WARNING) e non ferma
--    le altre.
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    begin
      execute format('alter table public.%I enable row level security', t.tablename);
      execute format('drop policy if exists backend_full_access on public.%I', t.tablename);
      execute format(
        'create policy backend_full_access on public.%I for all to postgres using (true) with check (true)',
        t.tablename
      );
    exception when others then
      raise warning 'RLS non applicata a %: %', t.tablename, sqlerrm;
    end;
  end loop;
end $$;

-- 2) Seconda cintura: i ruoli della Data API (anon, authenticated) non hanno
--    più privilegi sulle tabelle/sequenze/funzioni public, presenti e future
--    (per le funzioni anche PUBLIC, che ha EXECUTE di default).
revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from public, anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from public, anon, authenticated;

-- 3) Verifica A: ogni riga deve avere rowsecurity = true
select tablename, rowsecurity
from pg_tables
where schemaname = 'public'
order by tablename;

-- 4) Verifica B: deve restituire ZERO righe (nessuna tabella leggibile da
--    anon/authenticated)
select c.relname
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind in ('r', 'v', 'm', 'p')
  and (has_table_privilege('anon', c.oid, 'select')
       or has_table_privilege('authenticated', c.oid, 'select'));

-- 5) Verifica C: policy diverse da backend_full_access (attese: nessuna;
--    se ce ne sono, qualcos'altro usa la Data API su questo progetto)
select tablename, policyname, roles
from pg_policies
where schemaname = 'public' and policyname <> 'backend_full_access';
