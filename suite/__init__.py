"""reeld suite: the series → episode → publish pipeline (see SPEC.md).

Lives beside the legacy FastAPI reel editor (main.py) and shares its
database and `users` table. All suite state is stored in the database;
nothing here reads or writes project files (spec §9).
"""
