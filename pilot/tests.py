import os
from ConfigParser import SafeConfigParser

from app import PilotApplication
from byodr.utils import timestamp
from byodr.utils.testing import CollectPublisher, QueueReceiver, CollectServer


def create_application(config_dir):
    application = PilotApplication(config_dir=config_dir)
    application.publisher = CollectPublisher()
    application.teleop = QueueReceiver()
    application.vehicle = QueueReceiver()
    application.inference = QueueReceiver()
    application.ipc_chatter = QueueReceiver()
    application.ipc_server = CollectServer()
    return application


def test_create_and_setup(tmpdir):
    directory = str(tmpdir.realpath())
    app = create_application(directory)
    publisher, teleop, vehicle, ipc_chatter, ipc_server = app.publisher, app.teleop, app.vehicle, app.ipc_chatter, app.ipc_server
    try:
        # The default settings must result in a workable instance.
        app.setup()
        assert len(ipc_server.collect()) == 1
        assert not bool(ipc_server.get_latest())

        #
        # Switch to direct driver mode.
        teleop.add(dict(time=timestamp(), button_b=1))
        app.step()
        teleop.add(dict(time=timestamp()))
        vehicle.add(dict(time=timestamp()))
        app.step()
        status = publisher.get_latest()
        assert status.get('driver') == 'driver_mode.teleop.direct'
        map(lambda x: x.clear(), [teleop, vehicle, publisher])

        #
        # Change the configuration and request a restart.
        # Write a new config file.
        previous_process_frequency = app.get_process_frequency()
        new_process_frequency = previous_process_frequency + 10
        _parser = SafeConfigParser()
        _parser.add_section('pilot')
        _parser.set('pilot', 'clock.hz', str(new_process_frequency))
        with open(os.path.join(directory, 'test_config.ini'), 'wb') as f:
            _parser.write(f)
        #
        # Issue the restart request.
        ipc_chatter.add(dict(command='restart'))
        app.step()
        assert len(ipc_server.collect()) == 2
        assert not bool(ipc_server.get_latest())
        assert app.get_process_frequency() == new_process_frequency

        # The driver should still be in direct mode.
        teleop.add(dict(time=timestamp()))
        vehicle.add(dict(time=timestamp()))
        app.step()
        status = publisher.get_latest()
        assert status.get('driver') == 'driver_mode.teleop.direct'
        map(lambda x: x.clear(), [teleop, vehicle, publisher])
    finally:
        app.finish()
