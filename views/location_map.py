"""Shelf map: one grid per shelf, three positions a row, from saved locations such as A1 or A-1."""
import math
import pandas as pd
import streamlit as st

from stocker import db
from stocker.icons import icon

parse_location=db.parse_location   # 'A1' and 'A-1' both mean shelf A, position 1


def choose_product(sku,label):
    st.session_state.location_selected_sku=str(sku)
    st.session_state.inventory_product_selection=label


def render(products):
    st.subheader('Shelf map')
    st.caption('Choose a shelf, then select a position in its grid. A1 means Shelf A, position 1. Empty positions have no assigned products; the grid is a location index, not a measured floor plan.')
    parsed=products.location.fillna('').map(parse_location)
    areas=sorted({value[0] for value in parsed if value})
    if not areas:
        st.info('Save locations such as A1, A2 or B1 in the Products table to build the grid.')
        return
    label=lambda a:'Shelf '+a+(' · '+db.SHELVES[a] if a in db.SHELVES else '')
    area=st.selectbox('Shelf',areas,key='location_grid_area',format_func=label)
    selected_rows=products[[bool(v and v[0]==area) for v in parsed]]
    categories=', '.join(sorted(selected_rows.category.dropna().astype(str).unique()))
    st.markdown('**'+label(area)+'** · '+categories)
    # Three positions a row, at least three rows; a shelf with more products gets more rows. Empty slots retain their place.
    rows=max(3,math.ceil(max(v[1] for v in parsed if v and v[0]==area)/3))
    with st.container(border=True):
        for row_index in range(rows):
            cols=st.columns(3)
            for col_index,col in enumerate(cols):
                number=row_index*3+col_index+1
                code=f'{area}{number}'
                occupants=products[[bool(v and v==(area,number)) for v in parsed]]
                with col:
                    with st.container(border=True,height=175):
                        st.markdown('**'+code+'**')
                        if occupants.empty:
                            st.button('Empty position',key='grid_empty_'+code,disabled=True,width='stretch')
                            st.caption('No product assigned')
                        for item in occupants.itertuples():
                            sku=str(item.sku);name=str(item.name)
                            st.button(icon(name,item.category)+' '+name,key='map_product_'+sku,type='primary' if st.session_state.get('location_selected_sku')==sku else 'secondary',width='stretch',on_click=choose_product,args=(sku,name+' ('+sku+')'))
                            st.caption(f'{item.current_stock:g} units' if pd.notna(item.current_stock) else 'Stock not recorded')
    unmatched=int(parsed.map(lambda v:v is None).sum())
    if unmatched:st.caption(f'{unmatched} products have no valid grid position. Use a shelf letter and a number, e.g. A1; all products remain in the original table.')
    sku=st.session_state.get('location_selected_sku')
    hit=products[products.sku.astype(str)==str(sku)] if sku else products.iloc[:0]
    if not hit.empty:
        item=hit.iloc[0]
        with st.container(border=True):
            st.markdown('### Selected product · '+str(item['name']))
            a,b,c=st.columns(3)
            a.write('**Product code**');a.write(str(item['sku']))
            b.write('**Location**');b.write(str(item['location']))
            c.write('**Category**');c.write(str(item['category']))
            st.caption('Selected in the stock movement form above. Clicking a position does not change inventory.')
