from flask import Flask, session, request, redirect, jsonify, render_template, render_template_string
import hashlib
import tank_vol_fcns as TVF
import time
import os
import datetime as dt
import subprocess
import numpy as np
# from dotenv import load_dotenv

# Load environment variables from the credentials file
# load_dotenv(os.path.join(settings.path_to_repo, "flask_credentials.env"))

app = Flask(__name__)

# Get the secret key from the credentials file
# app.secret_key = os.getenv("FLASK_SECRET_KEY")

# if app.secret_key is None:
#     raise ValueError("No secret key found. Please ensure the credentials file is correctly set up.")


# Load credentials
# with open(os.path.join(settings.path_to_repo,'website_credentials.env'), 'r') as file:
#     credentials = file.read().splitlines()
#     USERNAME_HASH = credentials[0].strip()
#     PASSWORD_HASH = credentials[1].strip()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # username = request.form.get("username")
        # password = request.form.get("password")

        # if (hashlib.md5(username.encode()).hexdigest() == USERNAME_HASH and
        #         hashlib.md5(password.encode()).hexdigest() == PASSWORD_HASH):
        if True:
            session["authenticated"] = True
            return redirect("/")

    if session.get("authenticated"):
        return render_template("template.html")
    else:
        return render_template("template.html") 
        
@app.route('/update', methods=['GET','POST'])
def update():
    if request.method == "POST":
        action = request.json.get('command')
        print('received post request with action of: {}'.format(action))
        data = {}
        for name in TVF.tank_names:
            TVF.queue_dict[name]['command'].put(action)
            while TVF.queue_dict[name]['response'].empty():
                time.sleep(0.1)
            data[name] = TVF.queue_dict[name]['response'].get()
        data['system_time'] = dt.datetime.now().strftime('%Y-%m-%d %-I:%M%p')

        combined_gals = 0
        combined_rate = 0
        max_combined_gals = 0
        for tank_name in TVF.tank_names:
            combined_gals += data[tank_name]['current_gallons']
            if isinstance(data[tank_name]['rate'], (int, float, complex)):
                combined_rate += data[tank_name]['rate']
                data[tank_name]['rate'] = str(data[tank_name]['rate'])
            max_combined_gals += data[tank_name]['max_gallons']

        timing_est_str = 'N/A'
        if combined_rate > TVF.not_filling_emptying_buffer:
            if data['roadside']['filling']:
                remaining_capacity = data['roadside']['max_gallons'] - data['roadside']['current_gallons']
            else:
                remaining_capacity = max_combined_gals - combined_gals

            remaining_hrs = remaining_capacity/combined_rate

            remaining_time = dt.datetime.now()+dt.timedelta(hours=remaining_hrs)

            timing_est_str = 'Overflow roadside at {} ({}hrs)'.format(remaining_time.strftime('%Y-%m-%d %-I:%M%p'),np.round(remaining_hrs,1))

        if combined_rate < -TVF.not_filling_emptying_buffer:
            remaining_hrs = (combined_gals-TVF.last_fire_gallons)/np.abs(combined_rate)
            remaining_time = dt.datetime.now()+dt.timedelta(hours=remaining_hrs)

            timing_est_str = 'Last fire ({}gal) at {} ({}hrs)'.format(TVF.last_fire_gallons,remaining_time.strftime('%Y-%m-%d %-I:%M%p'),np.round(remaining_hrs,1))


        data['combined_gals'] = str(np.round(combined_gals,0))
        data['combined_rate'] = str(np.round(combined_rate,1))
        data['timing_est_str'] = str(timing_est_str)

        return jsonify(data)
    elif request.method == "GET":
        print('received get request')
        return  '''
                <!DOCTYPE html>
                <html>
                    <head>
                        <h1> HELLO WORLD </h1>
                    </head>
                    <body>
                        here be chickens
                    </body>
                </html>'''



@app.route("/confirm", methods=["GET", "POST"])
def confirm():
    if request.method == "POST":
        action = request.form.get("action", "Nothing").strip().lower()

        if action == "reset":
            for name in TVF.tank_names:
                TVF.queue_dict[name]['command'].put("reset_dataframe")
            return redirect("/")
        elif action == "reboot":
            subprocess.Popen(["sudo", "reboot", "now"])
            return "Rebooting..."  # Optionally render a "rebooting" page
        else:
            return redirect("/")

    return render_template_string("""
        <h1>What do you want to do?</h1>
        <form method="POST">
            <input type="text" name="action" value="Nothing" />
            <button type="submit">Submit</button>
        </form>
        <form action="/" method="GET">
            <button type="submit">Back</button>
        </form>
    """)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")