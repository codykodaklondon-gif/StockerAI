import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from streamlit.testing.v1 import AppTest
from stocker import db

class MapTests(unittest.TestCase):
    def test_cell_selects_exact_product_without_changing_stock(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(db,'DATA_DIR',Path(directory)):
            db.use_workspace('maptest');db.init()
            db.upsert_products(pd.DataFrame([dict(sku='A',name='Eggs',category='Dairy',location='A-1',current_stock=12),dict(sku='B',name='Milk',category='Dairy',location='A-2',current_stock=20),dict(sku='C',name='Tissue',category='Household',location='Back room',current_stock=30)]))
            at=AppTest.from_string("from stocker import db\nfrom views.setup import page_inventory\ndb.use_workspace('maptest')\npage_inventory()",default_timeout=30).run()
            self.assertFalse(at.exception,str(at.exception))
            self.assertEqual(sum(b.label=='Empty position' for b in at.button),7)
            next(b for b in at.button if b.key=='map_product_B').click().run()
            self.assertFalse(at.exception,str(at.exception))
            self.assertEqual(next(s for s in at.selectbox if s.label=='Product').value,'Milk (B)')
            self.assertTrue(any('Selected product · Milk' in x.value for x in at.markdown))
            self.assertTrue(any('1 products have no valid grid position' in x.value for x in at.caption))
            db.use_workspace('maptest');self.assertEqual(db.products().set_index('sku').current_stock.to_dict(),{'A':12,'B':20,'C':30})
