# Import statements
from time import sleep, time
from math import floor
import tkinter as tk
import json

# Raspi specific imports
from adafruit_motorkit import MotorKit
from adafruit_motor import stepper
from gpiozero import LED

##  GLOBAL CONSTANTS  ##
# - NEMA 17 data taken from motor datasheet
# - Lead screw pitch is as designed
NEMA_17_STEPS_PER_DEGREE = 3200.0 / 360.0
LEAD_SCREW_PITCH_IN_CM = 4.0

"""
StepperMotor Class

Used to keep track of stepper motor states. All angles in degrees
"""

class StepperMotor:
	def __init__(self, index, steps_per_degree, lead_screw_pitch, reverse=False):
		kit = MotorKit()
		
        # Index determines whether we are using the table or carriage motor
		self.motor = kit.stepper2 if index == 2 else kit.stepper1
		self.angle = 0.0
		self.steps_per_degree = steps_per_degree
		self.cm_per_deg = lead_screw_pitch / 360.0

        # Reverse keep track of whether the motor is reversed or not
        # That is, which way does it need to turn to push the slider
        # in a specific direction
		self.reverse = reverse

        # Keeps track of which direction the needle is moving during fractionation
		self.forwards = True
	
    # Get current angle of the motor shaft (unbounded)
	def get_angle(self):
		return self.angle
		
    # Tares the motor angle
	def tare(self):
		self.angle = 0.0
		
    # Releases the motor hold to prevent unnecessary energy usage and
    # overheating
	def release(self):
		self.motor.release()
	
    # Turn the motor shaft a number of degrees relative to its current position
	def move_relative(self, angle):
		backlash = 0.3 / self.cm_per_deg
		
		if self.forwards and angle < 0:
			angle -= backlash
			self.forwards = False
		elif not self.forwards and angle > 0:
			angle += backlash
			self.forwards = True
		
		steps_needed = floor(self.steps_per_degree * angle)
		
		# FORWARD if positive angle and not reversed or if negative angle and reversed
		
		direction = stepper.FORWARD if (angle > 0 and not self.reverse) or (angle < 0 and self.reverse) else stepper.BACKWARD
		
		for _ in range(0, abs(steps_needed)):
			self.motor.onestep(direction=direction, style=stepper.MICROSTEP)
			sleep(0.0001)
	
		self.angle = self.angle + steps_needed / self.steps_per_degree
		
		self.release()

    # Turn the motor shaft to a specific angle relative to its
    # initial starting position
	def move_absolute(self, angle):
		delta_angle = angle - self.angle
		self.move_relative(delta_angle)
	
    # Move the slider on this motor a number of centimeters
    # relative to its current position
	def move_dist_relative(self, dist):
		self.move_relative(dist / self.cm_per_deg)
		
    # Move the slider on this motor to a specific position
    # relative to its initial starting position (also in centimeters)
	def move_dist_absolute(self, dist):
		self.move_absolute(dist / self.cm_per_deg)
		
# Composite widget to take advantage of code reuse
# Consists of a label and a text edit for user input
class TextEntry:
	def __init__(self, window, text, row):
		self.label = tk.Label(text=text)
		self.label.grid(row=row, column=0, columnspan=1)
		self.var = tk.StringVar()
		self.entry = tk.Entry(window, textvariable=self.var)
		self.entry.grid(row=row, column=1, columnspan=2, sticky="we")
	
    # Get the current string that the user has entered
	def get(self):
		return self.var.get()
		
    # Set the text entry to a specific string value
	def set(self, text):
		self.entry.delete(0, tk.END)
		self.entry.insert(0, text)
		
    # Remove the widget from the screen
	def grid_forget(self):
		self.label.grid_forget()
		self.entry.grid_forget()
		
# Main application class
class App(tk.Tk):
	def __init__(self):
		super().__init__()
		self.title("Robotic Fractionator GUI v0.1")
		
        # Initialize in the automated fractionation mode
		self.mode = "Automated"
		self.mode_btn = tk.Button(self, text="Mode: Automated", command=self.cycle_mode)
		self.mode_btn.grid(row=0, column=0, columnspan=3, sticky="we")
		
		self.set_mode_automated(True)
	
    # Cycle between the different modes
    # Automated -> Manual -> Cleaning -> Automated
	def cycle_mode(self):
		if self.mode == "Automated":
			self.mode = "Manual"
			self.set_mode_manual()
		elif self.mode == "Manual":
			self.mode = "Cleaning"
			self.set_mode_cleaning()
		elif self.mode == "Cleaning":
			self.mode = "Automated"
			self.set_mode_automated(False)
		
		self.mode_btn["text"] = "Mode: " + self.mode
		
    # Handle changing the screen to automated mode
	def set_mode_automated(self, first):
        # If this is not the initial bootup of the app, 
        # remove the Cleaning widgets from the screen
		if not first:
			self.carriage_lbl.grid_forget()
			self.carriage_entry.grid_forget()
			self.movement_btn.grid_forget()
			self.pump_btn.grid_forget()
			self.progress_lbl.grid_forget()
        else:
			self.step_lbl = tk.Label(text="")
			self.step_forwards_btn = tk.Button(self, text="", command=lambda: self.manual_step(True))
			self.step_backwards_btn = tk.Button(self, text="", command=lambda: self.manual_step(False))
		
        # Add the JSON file select widgets
		self.json_lbl = tk.Label(text="Load well plate file: ")
		self.json_lbl.grid(row=1, column=0, columnspan=1)
		self.json_entry = tk.Entry(self)
		self.json_entry.grid(row=1, column=1, columnspan=1)
		self.json_btn = tk.Button(self, text="Load", command=self.load_json)
		self.json_btn.grid(row=1, column=2, columnspan=1, sticky="we")
		
        # Add the manual text entry widgets
		self.rows_text_entry = TextEntry(self, "Enter # of rows:", 2)
		self.cols_text_entry = TextEntry(self, "Enter # of columns:", 3)
		self.ws_text_entry = TextEntry(self, "Enter well size in cm:", 4)
		self.pump_rate_text_entry = TextEntry(self, "Enter pump rate in cc/hr:", 5)
		self.vol_text_entry = TextEntry(self, "Enter desired volume in cc:", 6)
		
        # Add the table/carriage movement entry widgets
		self.table_lbl = tk.Label(text="Move table to: ")
		self.table_lbl.grid(row=7, column=0, columnspan=1)
		self.carriage_lbl = tk.Label(text="Move carriage to: ")
		self.carriage_lbl.grid(row=8, column=0, columnspan=1)
		self.table_entry = tk.Entry(self)
		self.table_entry.grid(row=7, column=1, columnspan=1)
		self.carriage_entry = tk.Entry(self)
		self.carriage_entry.grid(row=8, column=1, columnspan=1)
		
        # Add the move button
		self.movement_btn = tk.Button(self, text="Move", command=self.set_table_carriage)
		self.movement_btn.grid(row=7, column=2, columnspan=1, rowspan=2, sticky="we")
		
        # Ensure the columns are laid out properly
		for i in range(3):
			self.grid_columnconfigure(i, weight=1, uniform="a")
			
        # Add the pump toggle button
		self.pump_btn = tk.Button(self, text="Toggle pump", command=self.toggle_pump)
		self.pump_btn.grid(row=9, column=0, columnspan=3, sticky="we")
		
        # Add the start button
		self.btn = tk.Button(self, text="Begin fractionation", command=self.run_checks)
		self.btn.grid(row=10, column=0, columnspan=3, sticky="we")
		
        # Add the pause button
		self.pause_btn =tk.Button(self, text="Click to pause", command = self.toggle_pause)
		self.pause_btn.grid(row=11, column=0,columnspan=3, sticky="we")
		self.is_paused = False
			
        # Add the progress label
		self.progress_lbl = tk.Label(text="System idle.")
		self.progress_lbl.grid(row=12, column=0, columnspan=3, sticky="we")

        # Add the canvas for showing current progress
		self.canvas = tk.Canvas(self, width=500, height=300, bd=0, highlightthickness=0)
		self.canvas.grid(row = 13, column = 0, columnspan=3)
			
        # Initialize the motor objects
		self.table_motor = StepperMotor(2, NEMA_17_STEPS_PER_DEGREE, LEAD_SCREW_PITCH_IN_CM, True)
		self.carriage_motor = StepperMotor(1, NEMA_17_STEPS_PER_DEGREE, LEAD_SCREW_PITCH_IN_CM, True)
		
        # Create the pump object if booting up
        # We use an LED object because digital output of 1 or 0
        # Is the same for the pump as it is for an LED
		if first:
			self.pump = LED("5")
		
        # Move the motors a small distance to better initialize
		self.table_motor.move_dist_relative(-0.1)
		self.carriage_motor.move_dist_relative(-0.1)
		self.pump_is_on = False
		
        # Define all variables
		self.ROWS = 0
		self.COLS = 0
		self.well_size = 0
		self.pump_time = 0.0
		
        # Keep track of taskID for pausing
		self.taskId = None
		self.state = "idle"
		
        # Current state of fractionation
		self.x = 0
		self.y = 0
		self.carriage_forwards = True
		
    # Handle changing the screen to manual mode
	def set_mode_manual(self):
        # Remove all Automated mode widgets
		self.json_lbl.grid_forget()
		self.json_entry.grid_forget()
		self.json_btn.grid_forget()
		self.rows_text_entry.grid_forget()
		self.cols_text_entry.grid_forget()
		self.ws_text_entry.grid_forget()
		self.pump_rate_text_entry.grid_forget()
		self.vol_text_entry.grid_forget()
		self.table_lbl.grid_forget()
		self.table_entry.grid_forget()
		self.pump_btn.grid_forget()
		self.btn.grid_forget()
		self.pause_btn.grid_forget()
		self.progress_lbl.grid_forget()
		self.canvas.grid_forget()
		self.carriage_lbl.grid_forget()
		self.carriage_entry.grid_forget()
		self.movement_btn.grid_forget()
		
		# Add the manual entry widgets
		self.rows_text_entry = TextEntry(self, "Enter # of rows:", 1)
		self.cols_text_entry = TextEntry(self, "Enter # of columns:", 2)
		self.ws_text_entry = TextEntry(self, "Enter well size in cm:", 3)
		
        # Add the table/carriage movement widgets
		self.table_lbl = tk.Label(text="Move table to: ")
		self.table_lbl.grid(row=4, column=0, columnspan=1)
		self.carriage_lbl = tk.Label(text="Move carriage to: ")
		self.carriage_lbl.grid(row=5, column=0, columnspan=1)
		self.table_entry = tk.Entry(self)
		self.table_entry.grid(row=4, column=1, columnspan=1)
		self.carriage_entry = tk.Entry(self)
		self.carriage_entry.grid(row=5, column=1, columnspan=1)
		
        # Add the Move button
		self.movement_btn = tk.Button(self, text="Move", command=self.set_table_carriage)
		self.movement_btn.grid(row=4, column=2, columnspan=1, rowspan=2, sticky="we")
		
        # Add the single step forwards/backwards buttons
		self.step_lbl = tk.Label(text="Move the needle one step:")
		self.step_lbl.grid(row=6, column=0, columnspan=1, sticky="we")
		self.step_forwards_btn = tk.Button(self, text="Forwards", command=lambda: self.manual_step(True))
		self.step_forwards_btn.grid(row=6, column=1, columnspan=1, sticky="we")
		self.step_backwards_btn = tk.Button(self, text="Backwards", command=lambda: self.manual_step(False))
		self.step_backwards_btn.grid(row=6, column=2, columnspan=1, sticky="we")
		
        # Make the column layout widths equal
		for i in range(3):
			self.grid_columnconfigure(i, weight=1, uniform="a")
			
        # Add pump toggle button
		self.pump_btn = tk.Button(self, text="Toggle pump", command=self.toggle_pump)
		self.pump_btn.grid(row=7, column=0, columnspan=3, sticky="we")
		
        # Add canvas for showing progress
		self.canvas = tk.Canvas(self, width=500, height=300, bd=0, highlightthickness=0)
		self.canvas.grid(row = 8, column = 0, columnspan=3)
		
        # Define variables 
		self.ROWS = 0
		self.COLS = 0
		self.well_size = 0
		
        # Current position of needle
		self.x = 0
		self.y = 0
		self.carriage_forwards = True
		
	# Handle changing the screen to cleaning mode
	def set_mode_cleaning(self):
        # Remove all Manual mode widgets
		self.rows_text_entry.grid_forget()
		self.cols_text_entry.grid_forget()
		self.ws_text_entry.grid_forget()
		self.pump_btn.grid_forget()
		self.canvas.grid_forget()
		self.movement_btn.grid_forget()
		self.step_lbl.grid_forget()
		self.step_forwards_btn.grid_forget()
		self.step_backwards_btn.grid_forget()
		
        # Add carriage movement widgets
		self.carriage_lbl = tk.Label(text="Move carriage to: ")
		self.carriage_lbl.grid(row=1, column=0, columnspan=1)
		self.carriage_entry = tk.Entry(self)
		self.carriage_entry.grid(row=1, column=1, columnspan=1)
		self.carriage_entry.delete(0, tk.END)
		self.carriage_entry.insert(0, "14.0")
		
        # Add move button
		self.movement_btn = tk.Button(self, text="Move", command=self.set_table_carriage)
		self.movement_btn.grid(row=1, column=2, columnspan=1, sticky="we")
		
		for i in range(3):
			self.grid_columnconfigure(i, weight=1, uniform="a")
			
        # Add pump toggle button
		self.pump_btn = tk.Button(self, text="Toggle pump", command=self.toggle_pump)
		self.pump_btn.grid(row=2, column=0, columnspan=3, sticky="we")
			
        # Add progress label
		self.progress_lbl = tk.Label(text="System idle.")
		self.progress_lbl.grid(row=3, column=0, columnspan=3, sticky="we")
		
        # Initialize
		self.table_motor.move_dist_relative(-0.1)
		self.carriage_motor.move_dist_relative(-0.1)
		self.pump_is_on = False
		self.carriage_forwards = True
			
    # Load custom specifications from a JSON file
    # Used Opentrons standard for this
	def load_json(self):
		json_spec = open(self.json_entry.get())
		data = json.load(json_spec)
	
		r = len(data["ordering"][0])
		c = len(data["ordering"])
	
		self.rows_text_entry.set(str(r))
		self.cols_text_entry.set(str(c))
		self.ws_text_entry.set(str(abs(data["wells"]["A1"]["y"] - data["wells"]["B1"]["y"]) / 10.0))
	
		self.table_entry.delete(0, tk.END)
		self.table_entry.insert(0, str(15 - data["wells"]["A1"]["x"] * 0.1))

		self.carriage_entry.delete(0, tk.END)
		self.carriage_entry.insert(0, str(0.1 * (data["dimensions"]["yDimension"] - data["wells"]["A1"]["y"]) - 0.5))
	
    # Move the table and carriage based on table/carriage entry values
	def set_table_carriage(self):
		
		if self.table_entry.get() != '':
			self.table_motor.move_dist_absolute(float(self.table_entry.get()))
		
		if self.carriage_entry.get() != '':
			self.carriage_motor.move_dist_absolute(float(self.carriage_entry.get()))
		
    # Turn pump on or off
    # If cleaning, set progress label as needed
	def toggle_pump(self):
	
		self.pump_is_on = not self.pump_is_on
		if self.pump_is_on:
			self.pump.on()
			if self.mode == "Cleaning":
				self.progress_lbl["text"] = "System cleaning."
		else:
			self.pump.off()
			if self.mode == "Cleaning":
				self.progress_lbl["text"] = "System idle."
			
    # Handle pause functionality
	def toggle_pause(self):
		self.is_paused = not self.is_paused
		
        # If we are now paused...
		if self.is_paused:
            # Cancel what is happening right now and force pump to off state
			if self.taskId is not None:
				self.after_cancel(self.taskId)
				self.pump.off()
			self.pause_btn["text"] = "Click to unpause"
			self.progress_lbl["text"] = "Fractionation paused..."
		# If we are now unpaused...
        else:
			self.pause_btn["text"] = "Click to pause"
			self.progress_lbl["text"] = "Fractionation in progress..."
			
            # Based on the current state, go to the next step in the
            # fractionation process
			if self.state == "pump":
				self.stop_pump()
			elif self.state == "wait":
				self.move()
			elif self.state == "move":
				self.pump()
				
	# Validate all inputs to make sure fractionation can begin
	def run_checks(self):
	
		self.ROWS = int(self.rows_text_entry.get())
		self.COLS = int(self.cols_text_entry.get())
		self.well_size = float(self.ws_text_entry.get())
	
		self.pump_time = float(self.vol_text_entry.get()) / (float(self.pump_rate_text_entry.get()) / 3600)
	
		if self.ROWS != 0 and self.COLS != 0 and self.well_size != 0 and self.pump_time != 0:
			self.movement()
			
    # Beginning portion of the fractionation
	def movement(self):
        # Show the current progress
		self.canvas.create_rectangle(0, 0, self.COLS * 25 + 5, self.ROWS * 25 + 5, fill="black")
		self.update()
	
        # Start off moving forwards
		self.carriage_forwards = True
	
		self.progress_lbl["text"] = "Fractionation in progress..."
		
		self.x = 0
		self.y = 0
		
		self.pump_liquid()
		
    # Move to the next well in the plate
	def move(self):
		self.state = "move"
		if self.carriage_forwards:
			self.y = self.y + 1
			if self.y < self.ROWS:
				self.carriage_motor.move_dist_relative(self.well_size)
			else:
                # If we are at the end of the column, go to the next column
				self.y = self.ROWS - 1
				self.table_motor.move_dist_relative(-self.well_size)
				self.x = self.x + 1
				self.carriage_forwards = not self.carriage_forwards
		else:
			self.y = self.y - 1
			if self.y >= 0:
				self.carriage_motor.move_dist_relative(-self.well_size)
			else:
                # If we are at the end of the column, go to the next column
				self.y = 0
				self.table_motor.move_dist_relative(-self.well_size)
				self.x = self.x + 1
				self.carriage_forwards = not self.carriage_forwards
			
        # If the user paused during the motor movement,
        # then stop the movement
		if self.is_paused:
			return
		
        # If there are no more columns...
		if self.x == self.COLS:
            # Return to the starting position
			self.progress_lbl["text"] = "Fractionation finished!"
			self.carriage_return()
		else:
			self.pump_liquid()
		
    # Turn on the pump for the time specified by the desired volume
	def pump_liquid(self):
		self.state = "pump"
		self.pump.on()

        # Show that the well is in progress by making it green
		x1, x2 = 5 + 25 * self.x, 25 + 25 * self.x
		y1, y2 = 5 + 25 * self.y, 25 + 25 * self.y
		self.canvas.create_rectangle(x1, y1, x2, y2, fill="green")
		
        self.taskId = self.after(round(self.pump_time * 1000), self.stop_pump)
	
    # Turn off the pump for the same amount of time it was on to prevent
    # drops from entering other wells
	def stop_pump(self):
		self.state = "wait"
		self.pump.off()

        # Show that the well is finished by making it blue
		x1, x2 = 5 + 25 * self.x, 25 + 25 * self.x
		y1, y2 = 5 + 25 * self.y, 25 + 25 * self.y
		self.canvas.create_rectangle(x1, y1, x2, y2, fill="blue")

		self.taskId = self.after(round(self.pump_time * 1000), self.move)
		
    # Return the needle to the starting position
	def carriage_return(self):
		self.table_motor.move_dist_absolute(0.0)
		self.carriage_motor.move_dist_absolute(0.0)
		
    # Move the needle a single step forwards or backwards
	def manual_step(self, forwards):
		
		self.ROWS = int(self.rows_text_entry.get())
		self.COLS = int(self.cols_text_entry.get())
		self.well_size = float(self.ws_text_entry.get())
		
        # Make the entire canvas again
        # We can do this because only one square needs to be highlighted
        # during manual mode
		self.canvas.create_rectangle(0, 0, self.COLS * 25 + 5, self.ROWS * 25 + 5, fill="black")
		for i in range(0, self.COLS):
			for j in range(0, self.ROWS):
				x1, x2 = 5 + 25 * i, 25 + 25 * i
				y1, y2 = 5 + 25 * j, 25 + 25 * j
				self.canvas.create_rectangle(x1, y1, x2, y2, fill="gray")
		
        # If invalid inputs, do not step
		if self.ROWS == 0 or self.COLS == 0 or self.well_size == 0:
			return
		
        # If stepping forwards...
		if forwards:
            # If we are at the end of the fractionation, highlight the square
            # and end early
			if self.x == self.COLS - 1 and self.y == (self.ROWS - 1 if self.COLS % 2 == 1 else 0):
				x1, x2 = 5 + 25 * self.x, 25 + 25 * self.x
				y1, y2 = 5 + 25 * self.y, 25 + 25 * self.y
				self.canvas.create_rectangle(x1, y1, x2, y2, fill="yellow")
		
				self.update()
				return
            
            # Move based on which way the carriage would normally
            # go in this column during an automated fractionation
			if self.carriage_forwards:
				self.y = self.y + 1
				if self.y < self.ROWS:
					self.carriage_motor.move_dist_relative(self.well_size)
				else:
                    # If at the end of the column, go to the next column
					self.y = self.ROWS - 1
					self.table_motor.move_dist_relative(-self.well_size)
					self.x = self.x + 1
					self.carriage_forwards = not self.carriage_forwards
			else:
				self.y = self.y - 1
				if self.y >= 0:
					self.carriage_motor.move_dist_relative(-self.well_size)
				else:
                    # If at the end of the column, go to the next column
					self.y = 0
					self.table_motor.move_dist_relative(-self.well_size)
					self.x = self.x + 1
					self.carriage_forwards = not self.carriage_forwards
		# If stepping backwards...
        else:
            # If we are at the beginning of the fractionation, highlight the square
            # and end early
			if self.x == 0 and self.y == 0:
				x1, x2 = 5 + 25 * self.x, 25 + 25 * self.x
				y1, y2 = 5 + 25 * self.y, 25 + 25 * self.y
				self.canvas.create_rectangle(x1, y1, x2, y2, fill="yellow")
		
				self.update()
				return
            
            # Move based on which way the carriage would normally
            # go in this column during an automated fractionation
			if self.carriage_forwards:
				self.y = self.y - 1
				if self.y >= 0:
					self.carriage_motor.move_dist_relative(-self.well_size)
				else:
                    # If at the end of the column, go to the next column
					self.y = 0
					self.table_motor.move_dist_relative(self.well_size)
					self.x = self.x - 1
					self.carriage_forwards = not self.carriage_forwards
			else:
				self.y = self.y + 1
				if self.y < self.ROWS:
					self.carriage_motor.move_dist_relative(self.well_size)
				else:
                    # If at the end of the column, go to the next column
					self.y = self.ROWS - 1
					self.table_motor.move_dist_relative(self.well_size)
					self.x = self.x - 1
					self.carriage_forwards = not self.carriage_forwards
		
        # Highlight the square after movement
		x1, x2 = 5 + 25 * self.x, 25 + 25 * self.x
		y1, y2 = 5 + 25 * self.y, 25 + 25 * self.y
		self.canvas.create_rectangle(x1, y1, x2, y2, fill="yellow")
		
        # Update the app to ensure the change to the canvas is visible
		self.update()

# Create the app object
app = App()

# Begin the event loop
app.mainloop()

# Once the loop is done and the application is closed,
# release the motors to prevent overheating
app.table_motor.release()
app.carriage_motor.release()
