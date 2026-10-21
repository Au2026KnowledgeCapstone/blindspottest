"""Deterministic working/broken builds for regression development, never real payments.

Run: python -m demo_app.regression_fixture 3100
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        broken = parts.path.startswith('/broken/')
        path = parts.path.removeprefix('/broken') if broken else parts.path
        prefix = '/broken' if broken else ''
        suffix = '?nan' if parts.query == 'nan' else ''
        if path == '/':
            body = ''.join(f'<a href="{prefix}/{name}">{name}</a> ' for name in ('cart', 'payment', 'anonymous', 'history-start'))
        elif path == '/cart':
            body = f'<h1>Cart fixture</h1><a id="go" href="{prefix}/quantity{suffix}">Review cart</a>'
        elif path == '/quantity':
            value = 'NaN' if broken and parts.query == 'nan' else '-600' if broken else '600'
            qty = '-1' if broken else '1'
            body = f'<h1>Cart review</h1><input id="quantity" value="{qty}"><p class="amount">${value}</p><strong id="total">${value}</strong>'
        elif path == '/payment':
            handler = "event.preventDefault()" if broken else ''
            body = f'''<h1>Payment</h1><input type="radio" id="paypal" name="method" value="PayPal" checked><label for="paypal">PayPal</label>
            <input type="radio" id="stripe" name="method" value="Stripe" onclick="{handler}"><label for="stripe">Stripe</label>
            <select id="shipping"><option value="standard">Standard</option><option value="express">Express</option></select>
            <input type="checkbox" id="terms"><label for="terms">Terms</label>
            <a id="go" href="{prefix}/payment-review">Continue</a>'''
        elif path == '/payment-review':
            chosen = '' if broken else 'checked'
            other = 'checked' if broken else ''
            body = f'<h1>Payment review</h1><input type="radio" name="method" value="PayPal" {other}><input id="chosen" type="radio" name="method" value="Stripe" {chosen}><p id="reviewed">Stripe</p>'
        elif path in ('/anonymous', '/history-start'):
            target = 'admin' if path == '/anonymous' else 'history'
            body = f'<h1>Anonymous fixture</h1><a id="go" href="{prefix}/{target}">Open protected page</a>'
        elif path == '/admin':
            body = '<main id="screen">Sign in required</main>'
            if broken:
                body += '<script>document.querySelector("main").remove(); setTimeout(()=>{throw new TypeError("Cannot read properties of null (reading name)")},0)</script>'
        elif path == '/history':
            body = '<main>Cannot read properties of null (reading token)</main>' if broken else '<main>Sign in required</main>'
        elif path == '/frame':
            body = '<h1>Frame fixture</h1><iframe id="embedded" src="/embedded"></iframe>'
        elif path == '/embedded':
            body = '<label for="choice">Choice</label><input id="choice" type="checkbox"><select id="pick"><option value="a">A</option><option value="b">B</option></select>'
        elif path == '/ambiguous':
            body = '<button>Continue</button><button>Continue</button>'
        elif path == '/sticky':
            body = '''<h1>Mutation on URL load</h1><a href="/sticky-home">Home</a><main></main>
            <script>
            if(location.search) localStorage.setItem('items','1');
            if(!sessionStorage.getItem('fixture'))sessionStorage.setItem('fixture','ready');
            function render(){document.querySelector('main').innerHTML=localStorage.getItem('items')==='1'
              ? '<button id="remove" type="button">Remove item</button>'
              : '<a id="after" href="/sticky-done">After removal</a>';
              const remove=document.querySelector('#remove');
              if(remove)remove.onclick=()=>{localStorage.setItem('items','0');render()};}
            render();</script>'''
        elif path in ('/sticky-home','/sticky-done'):
            body = '<h1>Done</h1>'
        elif path == '/choices':
            body = '''<h1>Choices</h1><select id="delivery"><option value="standard">Standard</option><option value="express">Express</option></select>
            <label><input type="radio" id="pay" name="method" value="paypal" checked>PayPal</label>
            <label><input type="radio" id="card" name="method" value="stripe">Stripe</label>
            <label><input type="checkbox" id="terms">Terms</label>
            <button type="button" onclick="location.href='/choices-done?delivery='+document.querySelector('#delivery').value+'&method='+document.querySelector('input:checked').value">Continue</button>'''
        elif path == '/choices-done':
            body = '<h1>Choice submitted</h1>'
        elif path == '/async-choice':
            body = '''<h1>Async quantity</h1><p id="unit">$600</p><p id="total">$600</p>
            <select id="qty" onchange="document.body.insertAdjacentHTML('beforeend','<span id=loading>Loading</span>');setTimeout(()=>{document.querySelector('#total').innerText='$'+(600*this.value);document.querySelector('#loading').remove()},150)">
            <option value="1">1</option><option value="2">2</option></select>'''
        elif path in ('/depth-a','/depth-b','/depth-c','/depth-d'):
            index=['/depth-a','/depth-b','/depth-c','/depth-d'].index(path)
            body='<h1>Depth fixture</h1>'
            if index<3:
                body+=f'<a href="/depth-{chr(ord("a")+index+1)}">Next</a>'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(f'<!doctype html><html><head><title>Regression fixture</title></head><body>{body}</body></html>'.encode())


def server(port=0):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('port', type=int, nargs='?', default=3100)
    args = parser.parse_args()
    app = server(args.port)
    print(f'Regression fixtures: http://127.0.0.1:{app.server_port} (working) and /broken/ (defective)', flush=True)
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
