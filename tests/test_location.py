import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from stocker import db

class LocationTests(unittest.TestCase):
    def test_migration_and_save_clear_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.db'
            old_schema=db.SCHEMA.replace("    location       TEXT DEFAULT '',\n",'')
            with sqlite3.connect(path) as con:
                con.executescript(old_schema)
                con.execute("INSERT INTO products(sku,name,category,current_stock) VALUES ('A','Milk','Dairy',12)")
            db.init(path);db.init(path)
            self.assertEqual(db.products(path).iloc[0]['current_stock'],12)
            self.assertEqual(db.products(path).iloc[0]['location'],'')
            db.upsert_products(pd.DataFrame([dict(sku='A',name='Milk',location=' A-02-01 ')]),path)
            db.upsert_products(pd.DataFrame([dict(sku='A',name='Milk updated')]),path)
            self.assertEqual(db.products(path).iloc[0]['location'],'A-02-01')
            db.upsert_products(pd.DataFrame([dict(sku='A',name='Milk',location=None)]),path)
            self.assertEqual(db.products(path).iloc[0]['location'],'')

    def test_original_table_displays_both_new_columns(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(db,'DATA_DIR',Path(directory)):
            db.use_workspace('locationtest');db.init()
            db.upsert_products(pd.DataFrame([dict(sku='A',name='Milk',category='Dairy',location='A-01-02',current_stock=12,shelf_life_days=10)]))
            at=AppTest.from_string("from stocker import db\nfrom views.setup import page_inventory\ndb.use_workspace('locationtest')\npage_inventory()",default_timeout=30).run()
            self.assertFalse(at.exception,str(at.exception))
            row=at.dataframe[0].value.iloc[0]
            self.assertEqual(row['location'],'A-01-02');self.assertEqual(row['lifespan_group'],'Short')
            next(b for b in at.button if b.label=='Save product changes').click().run()
            self.assertFalse(at.exception,str(at.exception))
            db.use_workspace('locationtest')
            self.assertEqual(db.products().iloc[0]['location'],'A-01-02')
