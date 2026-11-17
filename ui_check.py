from pathlib import Path
from playwright.sync_api import sync_playwright
import json
with sync_playwright() as p:
    browser=p.chromium.launch(channel='chrome',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100},device_scale_factor=1)
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto('http://127.0.0.1:8787/',wait_until='domcontentloaded')
    page.wait_for_function("document.querySelector('#coverage').textContent.includes('462')",timeout=20000)
    assert page.locator('#rows tr').count()<=25
    page.locator('#search').fill('BTC')
    page.wait_for_timeout(100)
    assert page.locator('#rows .symbol-btn').count()>0
    assert all('BTC' in t for t in page.locator('#rows .symbol-btn').all_text_contents())
    page.locator('#reset').click()
    page.locator('#venue').select_option('popdex')
    assert all('PopDEX' in t for t in page.locator('#rows .route').all_text_contents())
    page.locator('#trigger-card').click()
    assert page.locator('#filter').input_value()=='TRIGGERED'
    page.locator('#reset').click()
    page.locator('#sort').select_option('symbol')
    page.locator('#next').click()
    assert page.locator('#page-number').inner_text().startswith('2 /')
    page.locator('#reset').click()
    page.locator('#theme').click()
    assert page.locator('html').get_attribute('data-theme')=='light'
    page.locator('#theme').click()
    with page.expect_download() as d:page.locator('#export').click()
    d.value.save_as('output/ui-export.csv')
    assert Path('output/ui-export.csv').read_text(encoding='utf-8-sig').startswith('"')
    page.screenshot(path='output/radar-desktop.png',full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_timeout(200)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    page.screenshot(path='output/radar-mobile.png',full_page=True)
    page.set_viewport_size({'width':1440,'height':1100})
    page.locator('#rows .symbol-btn').first.click()
    assert page.locator('#chartkey').input_value()
    assert not errors,errors
    print(json.dumps({'browser_errors':errors,'search':'PASS','venue_filter':'PASS','summary_filter':'PASS','pagination':'PASS','theme':'PASS','csv':'PASS','mobile_overflow':'PASS','chart_selection':'PASS'}))
    browser.close()
