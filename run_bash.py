import subprocess

def run_bash_script(script_path):
   """Runs a bash script and returns its output."""
   try:
       result = subprocess.run([script_path], capture_output=True, text=True, check=True)
       return result.stdout
   except subprocess.CalledProcessError as e:
       return f"Error: {e.stderr}"

if __name__ == "__main__":
   script_path = "./bash_scripts/run_psql.sh"  # Path to your bash script
   output = run_bash_script(script_path)
   print(output)
