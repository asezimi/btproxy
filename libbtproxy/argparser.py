
import argparse,sys,os


parser = argparse.ArgumentParser(
                    description="""Bluetooth MiTM Proxy.
                                   For analyzing bluetooth connections actively. """
                    )

parser.add_argument('addr_master', help='Bluetooth address of target master device', nargs='?',default=None)
parser.add_argument('addr_slave', help='Bluetooth address of target slave device', nargs='?',default=None)

parser.add_argument('-a','--set-address', help='Address to set for Bluetooth adaptor (requires -i)', )
parser.add_argument('-n','--repair', help='Don\'t reuse existing paired connection', action='store_true')
parser.add_argument('-c','--copy-addresses', help='Copy the address of the target devices to adapters.  Will use the slave address if only using one adapter.  Useful for emulating some devices.  This may not work on some adapters.', action='store_true')

parser.add_argument('-i','--interface', help='Select a Bluetooth interface to use (for only using one adapter)', )
parser.add_argument('-s','--script', help='Pass a python script containing function definitions for master_cb and slave_cb for live manipulation of traffic', 
            type=lambda x: x if os.path.exists(x) else parser.error(x+' does not exist'))
parser.add_argument('-l','--list', help='List Adaptors', action='store_true')

parser.add_argument('-1','--master-name', help='Spoofed name to use for master adaptor', default=None)
parser.add_argument('-2','--slave-name', help='Spoofed name to use for slave adaptor', default=None)
parser.add_argument('-C','--slave-active', help='Spoofed slave adaptor will actively connect to'+
                                                ' master device instead of listening for a connection', action='store_true')
parser.add_argument('-v','--verbose', help='Print additional info for debugging', action="store_true")
parser.add_argument('-z','--no-sdp', help='Let bluetoothd run SDP normally and let btproxy advertise copied services. (Not completed)', action="store_true")

parser.add_argument('-q','--inquire-again', help='Inquire the services again, don\'t reuse saved settings.', action="store_true")
parser.add_argument('-u','--half-inquire', help='Skip inquiring master device. Useful if master device is not discoverable.', action="store_true")

args = parser.parse_args()
