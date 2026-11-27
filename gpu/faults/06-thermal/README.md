# Thermal slowdown

Ticket: Step time went from 1.9s to 2.6s. Nothing crashed and no one changed the code.

<details><summary>Answer</summary>

h100-03 GPU 5 is at ~90C with HW Thermal Slowdown active: SM clock ~1100 MHz versus ~1980 MHz on its peers. One slow GPU throttles the whole synchronous job (a straggler). Action: check the fans/airflow/inlet temperature in the BMC and any blocked filters, drain at the next checkpoint, then have DC ops inspect the node.

</details>
