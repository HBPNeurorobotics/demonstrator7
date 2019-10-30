# Imported transfer function that trains a predictive coding network live, and make predictions
from sensor_msgs.msg import Image
@nrp.MapRobotSubscriber('camera',       Topic('/camera/image_raw', Image))
@nrp.MapRobotPublisher( 'plot_topic',   Topic('/pred_plot',        Image))
@nrp.MapVariable(       'pred_msg',     initial_value=None)
@nrp.MapVariable(       'model',        initial_value=None)
@nrp.MapVariable(       'model_path',   initial_value=None)
@nrp.MapVariable(       'model_inputs', initial_value=None)
@nrp.MapVariable(       'optimizer',    initial_value=None)
@nrp.MapVariable(       'scheduler',    initial_value=None)
@nrp.MapVariable(       'run_step',     initial_value=0   )
@nrp.MapVariable(       'last_frame',   initial_value=None)
@nrp.Robot2Neuron()
def img_to_pred(t, camera, plot_topic, pred_msg, model, model_path,
    model_inputs, optimizer, scheduler, run_step, last_frame):

    # Imports and paths
    import os
    import torch
    import numpy
    from prednet   import PredNet
    from cv_bridge import CvBridge
    from specs     import localize

    # Image and model parameters
    underSmpl      = 5      # Avoiding too sharp time resolution (no change between frames)
    nt             = 10     # Number of "past" frames given to the network
    t_extrap       = 5      # After this frame, input is not used for future predictions
    n_feat         = 6      # Factor for number of features used in the network
    max_pix_value  = 1.0
    normalizer     = 255.0/max_pix_value
    C_channels     = 3  #  1 or 3 (color channels)
    A_channels     = (C_channels, n_feat*4, n_feat*8, n_feat*16, n_feat*8, n_feat*4, n_feat*2)
    R_channels     = (C_channels, n_feat*4, n_feat*8, n_feat*16, n_feat*8, n_feat*4, n_feat*2)
    local_path     = '/resources/model'+str(n_feat)+'.pt'
    new_model_path = os.getcwd()+local_path
    trained_w_path = os.environ['HBP']+'/Experiments/demonstrator6'+local_path
    device         = 'cpu'
    if torch.cuda.is_available():
        device = 'cuda'

    # Training parameters
    use_new_w      = False  # Do not use weights that are saved in new_model_path
    use_trained_w  = True   # If above is False, use trained_w_path as model weights
    do_train       = False  # Train with present frames if True, predicts future if False
    initial_lr     = 0.001  # Then, the learning rate is scheduled with cosine annealing
    epoch_loop     = 100    # Every epoch_loop, a prediction is made, to monitor progress
    n_batches      = 1      # For now, not usable (could roll images for multiple batches)
    
    # Check that the simulation frame is far enough
    if camera.value is not None and int(t*50) % underSmpl == 0:

        # Collect input image and initialize the network input
        cam_img = CvBridge().imgmsg_to_cv2(camera.value, 'rgb8')/normalizer
        if C_channels == 3:
            cam_img = torch.tensor(cam_img, device=device).permute(2,1,0)  # --> channels last
        if C_channels == 1:
            cam_img = cam_img[:,:,1]  # .mean(axis=2)
            cam_img = torch.tensor(cam_img, device=device).unsqueeze(dim=2).permute(2,1,0)
        img_shp = cam_img.shape
        cam_img = F.pad(cam_img, (8,8), 'constant', 0.0)  # makes input cols 256 instead of 240
        if model_inputs.value is None:
            model_inputs.value = torch.zeros((1,nt)+img_shp, device=device)

        # Update the model or the mode, if needed
        run_step.value = run_step.value + 1
        if new_model_path != model_path.value:

            # Update the model path if new or changed and reset prediction plot
            model_path.value = new_model_path
            pred_msg.value   = torch.ones(img_shp[0], img_shp[1]*t_extrap, img_shp[2]+10)*64.0

            # Load or reload the model
            model.value = PredNet(R_channels, A_channels, device=device, t_extrap=t_extrap)
            if device == 'cuda': model.value = model.value.to('cuda')
            if run_step.value == 1:
                try:
                    if use_new_w:
                        a = 1./0.
                    if use_trained_w:
                        model.value.load_state_dict(torch.load(trained_w_path))
                        clientLogger.info('Model initialized with pre-trained weights.')
                    else:
                        model.value.load_state_dict(torch.load(model_path.value))
                        clientLogger.info('Learning weights loaded in the model.')
                except:
                    clientLogger.info('No existing weight file found. Model initialized randomly.')
            
        # Initialize some variables needed for training
        time_loss_w = [1.0/(nt-1) if s > 0 else 0.0 for s in range(nt)]
        if t_extrap < nt:
            time_loss_w = [w if n < t_extrap else 2.0*w for n, w in enumerate(time_loss_w)]

        if None in [optimizer.value, scheduler.value]:
            optimizer.value = torch.optim.Adam(model.value.parameters(), lr=initial_lr)
            scheduler.value = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer.value, T_0=50)

        # Save the model at each epoch
        if run_step.value % epoch_loop == 1:
            torch.save(model.value.state_dict(), model_path.value)

        # Check that the model exists and initialize plot message
        if model.value is not None:

            # Feed network and train it or compute prediction
            model_inputs.value = model_inputs.value.roll(-1, dims=1)
            model_inputs.value[0,-1,:,:,:] = cam_img
            if run_step.value > nt:

                # Compute prediction along present frames and updates weights
                if do_train:

                    # Compute prediction loss for every frame
                    pred, _ = model.value(model_inputs.value, nt)
                    loss    = torch.tensor([0.0], device=device)
                    for s in range(nt):
                        error = (pred[s][0] - model_inputs.value[0][s])**2
                        loss += torch.sum(error)*time_loss_w[s]

                    # Backward pass and weight updates
                    optimizer.value.zero_grad()
                    loss.backward()
                    optimizer.value.step()
                    scheduler.value.step()

                # Predicts future frames without weight updates
                else:
                    with torch.no_grad():
                        pred, states = model.value(model_inputs.value[:,-t_extrap:,:,:,:], nt)

                    clientLogger.info(str(len(states)) + ' ' + str(states[0].shape))

                # Collect prediction frames
                target_locations = []
                for s in range(t_extrap):
                    to_display, loc = localize(torch.detach(pred[s+nt-t_extrap][0]).cpu())
                    target_locations.append((s, loc))
                    pred_msg.value[:,s*img_shp[1]:(s+1)*img_shp[1],:img_shp[2]] = to_display

                # Print loss or prediction messages
                if do_train:
                    clientLogger.info('Epoch: %2i - step: %2i - error: %5.4f - lr: %5.4f' % \
                        (int(run_step.value/epoch_loop), run_step.value%epoch_loop, loss.item(), \
                         scheduler.value.get_lr()[0]))
                else:
                    clientLogger.info('Target locations: ' + str(target_locations))

            # Collect input frames
            inpt_msg = torch.zeros(img_shp[0], img_shp[1]*t_extrap, img_shp[2])
            for s in range(t_extrap):
                inpt_msg[:,s*img_shp[1]:(s+1)*img_shp[1],:] = model_inputs.value[0,s+nt-t_extrap]

            # Build and display the final message
            plot_msg = torch.cat((pred_msg.value, inpt_msg), 2).numpy().transpose(2,1,0)*int(normalizer)
            if C_channels == 1:
                plot_msg = numpy.dstack((plot_msg, plot_msg, plot_msg))
            plot_topic.send_message(CvBridge().cv2_to_imgmsg(plot_msg.astype(np.uint8),'rgb8'))
