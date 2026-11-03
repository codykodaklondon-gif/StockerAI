import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from stocker import db
from stocker.lifespan import lifespan_group


class LifespanTests(unittest.TestCase):
    def test_boundaries_and_unknown_values(self):
        for value, expected in [(1,'Short'),(30,'Short'),(31,'Medium'),(180,'Medium'),(181,'Long'),(30.5,'Medium'),(None,'Unknown'),(float('nan'),'Unknown'),(float('inf'),'Unknown'),(0,'Unknown'),(-1,'Unknown'),('invalid','Unknown')]:
            with self.subTest(value=value): self.assertEqual(lifespan_group(value),expected)

    def test_inventory_editor_keeps_original_columns_and_saves_without_derived_field(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db,'DATA_DIR',Path(directory)):
            db.use_workspace('lifespantest');db.init()
            db.upsert_products(pd.DataFrame([dict(sku='TEST',name='Sample',category='Household',current_stock=8,unit_cost=2,unit_price=3,lead_time_days=2,shelf_life_days=30)]))
            source="""import streamlit as st
from stocker import db
from views.setup import page_inventory
db.use_workspace('lifespantest')
page_inventory()
"""
            at=AppTest.from_string(source,default_timeout=30).run()
            self.assertFalse(at.exception,str(at.exception))
            df=at.dataframe[0].value
            self.assertEqual(df.iloc[0]['lifespan_group'],'Short')
            self.assertEqual(df.iloc[0]['category'],'Household')
            self.assertTrue(set(['sku','name','category','current_stock','unit_cost','unit_price','lead_time_days','shelf_life_days']).issubset(df.columns))
            next(b for b in at.button if b.label=='Save product changes').click().run()
            self.assertFalse(at.exception,str(at.exception))
            db.use_workspace('lifespantest')
            self.assertEqual(db.products().iloc[0]['current_stock'],8)
            row=db.products();row['shelf_life_days']=181;db.upsert_products(row)
            at.run();self.assertEqual(at.dataframe[0].value.iloc[0]['lifespan_group'],'Long')
