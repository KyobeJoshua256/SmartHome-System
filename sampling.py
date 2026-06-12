from HomeServer import create_app
app, _= create_app()
for rule in app.url_map.iter_rules():
    if 'guest' in rule.endpoint:
        print(rule.endpoint, rule)